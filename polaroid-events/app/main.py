import logging
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from . import cloud
from .imaging import MAX_BYTES, render_photo

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    cloud.initialize()
    yield


app = FastAPI(title='Álbum de eventos', lifespan=lifespan)


@app.exception_handler(Exception)
async def internal_error(request, exc):
    # No devolver secretos, SQL, contraseñas ni mensajes completos del proveedor.
    logger.error('Fallo interno en %s (%s)', request.url.path, type(exc).__name__)
    return JSONResponse(status_code=503, content={'detail': 'Servicio no disponible; revisa la configuración y reintenta.'})


class EventInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    client_name: str = Field(min_length=1, max_length=120)
    event_type: str = Field(min_length=1, max_length=80)
    event_date: date


class FinishInput(BaseModel):
    event_id: UUID


def locked_event(conn, event_id):
    event = conn.execute('SELECT * FROM events WHERE event_id = %s FOR UPDATE',
                         (event_id,)).fetchone()
    if not event:
        raise HTTPException(404, 'Evento no encontrado.')
    return event


@app.post('/events', status_code=201)
def create_event(body: EventInput):
    event_id = uuid4()
    with cloud.connect() as conn:
        conn.execute('''INSERT INTO events(event_id, client_name, event_type, event_date)
                        VALUES (%s, %s, %s, %s)''',
                     (event_id, body.client_name, body.event_type, body.event_date))
    return {'event_id': str(event_id)}


@app.get('/events/{event_id}')
def get_event(event_id: UUID):
    with cloud.connect() as conn:
        event = conn.execute('''SELECT e.*, count(p.photo_id) AS photo_count
            FROM events e LEFT JOIN photos p ON e.event_id = p.event_id
            WHERE e.event_id = %s GROUP BY e.event_id''', (event_id,)).fetchone()
    if not event:
        raise HTTPException(404, 'Evento no encontrado.')
    return event


@app.post('/upload', status_code=201)
def upload(event_id: UUID = Form(...), message: str = Form(...),
           photo: UploadFile = File(...)):
    message = message.strip()
    if not message or len(message) > 300:
        raise HTTPException(422, 'El mensaje debe tener de 1 a 300 caracteres.')
    try:
        data = photo.file.read(MAX_BYTES + 1)
        picture, polaroid = render_photo(data, message)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        photo.file.close()
    photo_id = uuid4()
    picture_key = f'pictures/{event_id}/{photo_id}.jpg'
    polaroid_key = f'polaroids/{event_id}/{photo_id}.jpg'
    uploaded = []
    try:
        with cloud.connect() as conn:
            event = locked_event(conn, event_id)
            if event['status'] != 'open':
                raise HTTPException(409, 'El evento ya está finalizado.')
            for key, payload in [(picture_key, picture), (polaroid_key, polaroid)]:
                uploaded.append(key)
                cloud.s3().put_object(Bucket=cloud.bucket(), Key=key, Body=payload,
                                      ContentType='image/jpeg')
            conn.execute('''INSERT INTO photos(photo_id, event_id, message, picture_key, polaroid_key)
                VALUES (%s, %s, %s, %s, %s)''',
                (photo_id, event_id, message, picture_key, polaroid_key))
    except Exception:
        # Compensación: S3 y PostgreSQL no comparten una transacción distribuida.
        if uploaded:
            try:
                cloud.delete_objects(uploaded)
            except Exception:
                logger.error('Revisar objetos huérfanos para photo_id=%s', photo_id)
        raise
    return {'photo_id': str(photo_id), 'event_id': str(event_id), 'message': message,
            'picture_key': picture_key, 'polaroid_key': polaroid_key}


@app.post('/finish')
def finish(body: FinishInput):
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with cloud.connect() as conn:
            locked_event(conn, body.event_id)
            photos = conn.execute('SELECT * FROM photos WHERE event_id = %s ORDER BY created_at, photo_id',
                                  (body.event_id,)).fetchall()
            if not photos:
                raise HTTPException(409, 'El evento no tiene fotos.')
            # Primero completar el ZIP; si falta una polaroid no borrar los originales.
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
                for photo in photos:
                    obj = cloud.s3().get_object(Bucket=cloud.bucket(), Key=photo['polaroid_key'])
                    try:
                        output.writestr(f"{photo['photo_id']}.jpg", obj['Body'].read())
                    finally:
                        obj['Body'].close()
            cloud.delete_objects([p['picture_key'] for p in photos if not p['original_deleted']])
            conn.execute('UPDATE photos SET original_deleted = TRUE WHERE event_id = %s', (body.event_id,))
            conn.execute("""UPDATE events SET status = 'finished',
                finished_at = COALESCE(finished_at, now()) WHERE event_id = %s""", (body.event_id,))
        archive.seek(0)
    except Exception:
        archive.close()
        raise

    def chunks():
        try:
            while chunk := archive.read(64 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(chunks(), media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="event-{body.event_id}.zip"'})
