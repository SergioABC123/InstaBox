"""Pruebas del flujo con SQL/S3 simulados: no afirman un despliegue real."""
import io
import zipfile
from uuid import uuid4
from contextlib import contextmanager

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app import cloud
from app.main import app
from app.imaging import render_photo


def photo_bytes():
    stream = io.BytesIO()
    Image.new('RGB', (800, 500), '#3688a0').save(stream, format='JPEG')
    return stream.getvalue()


class FakeDB:
    def __init__(self):
        self.events, self.photos, self.result = {}, [], []

    def execute(self, sql, params=()):
        if sql.startswith('INSERT INTO events'):
            eid, client, kind, day = params
            self.events[eid] = dict(event_id=eid, client_name=client, event_type=kind,
                                    event_date=day, status='open')
        elif 'count(p.photo_id)' in sql:
            event = self.events.get(params[0])
            self.result = [dict(event, photo_count=sum(p['event_id'] == params[0] for p in self.photos))] if event else []
        elif sql.startswith('SELECT * FROM events'):
            event = self.events.get(params[0])
            self.result = [event] if event else []
        elif sql.startswith('INSERT INTO photos'):
            self.photos.append(dict(zip(['photo_id','event_id','message','picture_key','polaroid_key'], params), original_deleted=False))
        elif sql.startswith('SELECT * FROM photos'):
            self.result = [p for p in self.photos if p['event_id'] == params[0]]
        elif sql.startswith('UPDATE photos'):
            for p in self.photos:
                if p['event_id'] == params[0]:
                    p['original_deleted'] = True
        elif sql.startswith('UPDATE events'):
            self.events[params[0]]['status'] = 'finished'
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}

    def delete_objects(self, Bucket, Delete):
        for item in Delete['Objects']:
            self.objects.pop(item['Key'], None)
        return {}


@pytest.fixture
def setup(monkeypatch):
    db, storage = FakeDB(), FakeS3()
    @contextmanager
    def connect():
        yield db
    monkeypatch.setattr(cloud, 'initialize', lambda: None)
    monkeypatch.setattr(cloud, 'connect', connect)
    monkeypatch.setattr(cloud, 's3', lambda: storage)
    monkeypatch.setattr(cloud, 'bucket', lambda: 'test')
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, storage


def create(client):
    result = client.post('/events', json={'client_name': 'Ana', 'event_type': 'Boda', 'event_date': '2026-10-10'})
    assert result.status_code == 201
    return result.json()['event_id']


def upload(client, eid):
    return client.post('/upload', data={'event_id': eid, 'message': '¡Felicidades, corazón!'},
                       files={'photo': ('test.jpg', photo_bytes(), 'image/jpeg')})


def test_full_flow_and_isolation(setup):
    client, storage = setup
    a, b = create(client), create(client)
    for _ in range(3):
        assert upload(client, a).status_code == 201
    assert upload(client, b).status_code == 201
    assert client.get(f'/events/{a}').json()['photo_count'] == 3
    assert client.get(f'/events/{b}').json()['photo_count'] == 1
    result = client.post('/finish', json={'event_id': a})
    assert result.status_code == 200
    assert result.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert len(archive.namelist()) == 3
        for name in archive.namelist():
            assert Image.open(io.BytesIO(archive.read(name))).size == (1020, 1320)
    assert not any(k.startswith(f'pictures/{a}/') for k in storage.objects)
    assert sum(k.startswith(f'pictures/{b}/') for k in storage.objects) == 1
    assert client.get(f'/events/{a}').json()['status'] == 'finished'
    assert upload(client, a).status_code == 409
    assert client.post('/finish', json={'event_id': a}).status_code == 200


def test_invalid_and_empty(setup):
    client, storage = setup
    eid = create(client)
    assert client.post('/finish', json={'event_id': eid}).status_code == 409
    assert client.get(f'/events/{uuid4()}').status_code == 404
    assert client.get('/events/bad-uuid').status_code == 422
    assert client.post('/upload', data={'event_id': eid, 'message': 'hola'},
                       files={'photo': ('x.jpg', b'not an image')}).status_code == 422
    assert not storage.objects


def test_missing_polaroid_preserves_originals(setup):
    client, storage = setup
    eid = create(client)
    result = upload(client, eid).json()
    del storage.objects[result['polaroid_key']]
    assert client.post('/finish', json={'event_id': eid}).status_code == 503
    assert result['picture_key'] in storage.objects
    assert client.get(f'/events/{eid}').json()['status'] == 'open'


def test_image_dimensions_and_message():
    small, large = render_photo(photo_bytes(), '¡Felicidades, Ana!')
    assert Image.open(io.BytesIO(small)).size == (128, 128)
    image = Image.open(io.BytesIO(large))
    assert image.size == (1020, 1320)
    assert image.getpixel((10, 10)) == (255, 255, 255)
    assert image.crop((60, 990, 960, 1080)).convert('L').getextrema()[0] < 100
