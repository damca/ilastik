from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Thread
from typing import Dict, List
import io
import json
import os
import flask
from flask import Flask, request, Response, send_file
from flask_cors import CORS
import uuid
import numpy as np
from PIL import Image as PilImage

from ndstructs import Point5D, Slice5D, Shape5D, Array5D
from ndstructs.datasource import DataSource
from ilastik.annotations import Annotation, Scribblings
from ilastik.classifiers.pixel_classifier import PixelClassifier, StrictPixelClassifier, Predictions
from ndstructs.datasource import PilDataSource
from ilastik.features.feature_extractor import FeatureExtractor
from ilastik.features.vigra_features import GaussianSmoothing, HessianOfGaussian
from ilastik.utility import flatten, unflatten, listify

app = Flask("WebserverHack")
CORS(app)
feature_extractor_classes = [FeatureExtractor, HessianOfGaussian, GaussianSmoothing]
workflow_classes = {klass.__name__: klass for klass in [PixelClassifier, DataSource, Annotation] + feature_extractor_classes}

class Context:
    objects = {}

    @classmethod
    def do_rpc(cls):
        request_payload = cls.get_request_payload()
        obj = cls.load(request_payload.pop('self'))

    @classmethod
    def get_class_named(cls, name:str):
        return workflow_classes[name.title().replace('_', '')]

    @classmethod
    def create(cls, klass):
        request_payload = cls.get_request_payload()
        obj = klass.from_json_data(request_payload)
        key = cls.store(request_payload.get('id'), obj)
        return obj, key

    @classmethod
    def load(cls, key):
        return cls.objects[key]

    @classmethod
    def store(cls, obj_id, obj):
        obj_id = obj_id if obj_id is not None else str(hash(obj)) #FIXME hashes have to be stable between versions for this to work
        key = f"pointer@{obj_id}"
        cls.objects[key] = obj
        return key

    @classmethod
    def remove(cls, klass: type, key):
        target_class = cls.objects[key].__class__
        if not issubclass(target_class, klass):
            raise Exception(f"Unexpected class {target_class} when deleting object with key {key}")
        return cls.objects.pop(key)

    @classmethod
    def get_request_payload(cls):
        payload = {}
        for k, v in request.form.items():
            if isinstance(v, str) and v.startswith('pointer@'):
                payload[k] = cls.load(v)
            else:
                payload[k] = v
        for k, v in request.files.items():
            payload[k] = v.read()
        return listify(unflatten(payload))

    @classmethod
    def get_all(cls, klass) -> Dict[str, object]:
        return {key: obj for key, obj in cls.objects.items() if isinstance(obj, klass)}

def hacky_get_only_datasource():
    ds  = None
    for obj in Context.objects.values():
        if not isinstance(obj, DataSource):
            continue
        if ds is not None:
            raise Exception("There is more than ONE DataSource in the server.")
        ds = obj
    return ds

@app.route('/lines/', methods=['POST'])
def create_line_annotation():
    request_payload = Context.get_request_payload()
    print(f"Got this payload: ", json.dumps(request_payload, indent=4, default=str))

    int_vec_color =  tuple(int(v) for v in request_payload['color'])
    hashed_color = hash(int_vec_color) % 255

    voxels  = [Point5D.from_json_data(coords) for coords in request_payload['voxels']]

    min_point = Point5D(**{key: min(vox[key] for vox in voxels) for key in 'xyz'})
    max_point = Point5D(**{key: max(vox[key] for vox in voxels) for key in 'xyz'})

    # +1 because slice.stop is exclusive, but pointA and pointB are inclusive
    scribbling_roi = Slice5D.zero(**{key: slice(min_point[key],  max_point[key] + 1) for key in 'xyz'})
    scribblings = Scribblings.allocate(scribbling_roi, dtype=np.uint8, value=0)

    for voxel in voxels:
        colored_point = Scribblings.allocate(Slice5D.zero().translated(voxel), dtype=np.uint8, value=hashed_color)
        scribblings.set(colored_point)

    annotation = Annotation(scribblings=scribblings, #datasource=Context.load(request_payload['datasource_id'])
                            raw_data=request_payload['raw_data'])
    annotation_id = Context.store(request_payload.get('id'), annotation)
    return flask.jsonify(annotation_id)

def do_predictions(roi:Slice5D, classifier_id:str, datasource_id:str) -> Predictions:
    classifier = Context.load(classifier_id)
    full_data_source = Context.load(datasource_id)
    clamped_roi = roi.clamped(full_data_source)
    data_source = full_data_source.resize(clamped_roi)

    predictions = classifier.allocate_predictions(data_source)
    with ThreadPoolExecutor() as executor:
        for raw_tile in data_source.get_tiles():
            def predict_tile(tile):
                tile_prediction, tile_features = classifier.predict(tile)
                predictions.set(tile_prediction, autocrop=True)
            executor.submit(predict_tile, raw_tile)
    return predictions

@app.route('/predict/', methods=['GET'])
def predict():
    roi_params = {}
    for axis, v in request.args.items():
        if axis in 'tcxyz':
            start, stop = [int(part) for part in v.split('_')]
            roi_params[axis] = slice(start, stop)

    predictions = do_predictions(roi=Slice5D(**roi_params),
                                 classifier_id=request.args['pixel_classifier_id'],
                                 datasource_id=request.args['data_source_id'])

    channel=int(request.args.get('channel', 0))
    out_image = predictions.as_pil_images()[channel]
    out_file = io.BytesIO()
    out_image.save(out_file, 'png')
    out_file.seek(0)
    return send_file(out_file, mimetype='image/png')

#https://github.com/google/neuroglancer/tree/master/src/neuroglancer/datasource/precomputed#unsharded-chunk-storage
@app.route('/predictions/<classifier_id>/<datasource_id>/data/<int:xBegin>-<int:xEnd>_<int:yBegin>-<int:yEnd>_<int:zBegin>-<int:zEnd>')
def ngpredict(classifier_id:str, datasource_id:str, xBegin:int, xEnd:int, yBegin:int, yEnd:int, zBegin:int, zEnd:int):
    requested_roi = Slice5D(x=slice(xBegin, xEnd), y=slice(yBegin, yEnd), z=slice(zBegin, zEnd))
    predictions = do_predictions(roi=requested_roi,
                                 classifier_id=classifier_id,
                                 datasource_id=datasource_id)

    # https://github.com/google/neuroglancer/tree/master/src/neuroglancer/datasource/precomputed#raw-chunk-encoding
    # "(...) data for the chunk is stored directly in little-endian binary format in [x, y, z, channel] Fortran order"
    resp = flask.make_response(predictions.as_uint8().raw('xyzc').tobytes('F'))
    resp.headers['Content-Type'] = 'application/octet-stream'
    return resp

@app.route('/predictions/<classifier_id>/<datasource_id>/info')
def info_dict(classifier_id:str, datasource_id:str) -> Dict:
    classifier = Context.load(classifier_id)
    datasource = Context.load(datasource_id)

    expected_predictions_shape = classifier.get_expected_roi(datasource).shape

    resp = flask.jsonify({
        "@type": "neuroglancer_multiscale_volume",
        "type": "image",
        "data_type": "uint8", #DONT FORGET TO CONVERT PREDICTIONS TO UINT8!
        "num_channels": int(expected_predictions_shape.c),
        "scales": [
            {
                "key": "data",
                "size": [int(v) for v in expected_predictions_shape.to_tuple('xyz')],
                "resolution": [1,1,1],
                "voxel_offset": [0,0,0],
                "chunk_sizes": [[64, 64, 64]],
                "encoding": "raw",
            },
        ],
    })
    return resp

@app.route('/<class_name>/<object_id>', methods=['DELETE'])
def remove_object(class_name, object_id:str):
    Context.remove(Context.get_class_named(class_name), line_id)
    return jsonify({'id': line_id})

@app.route("/<class_name>/", methods=['POST'])
def create_object(class_name:str):
    obj, uid = Context.create(Context.get_class_named(class_name))
    return json.dumps(uid)

@app.route('/<class_name>/', methods=['GET'])
def list_objects(class_name):
    klass = Context.get_class_named(class_name)
    return flask.jsonify({ext_id: ext.json_data for ext_id, ext in Context.get_all(klass).items()})

@app.route('/<class_name>/<object_id>', methods=['GET'])
def show_object(class_name:str, object_id:str):
    klass = Context.get_class_named(class_name)
    return flask.jsonify(Context.load(object_id).json_data)


#Thread(target=partial(app.run, host='0.0.0.0')).start()
Thread(target=app.run).start()
