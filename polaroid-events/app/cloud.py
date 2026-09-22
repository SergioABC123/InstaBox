"""Solo configuración pública en entorno; credenciales de RDS desde el secret."""
import json
import os
from functools import lru_cache
from pathlib import Path

import boto3
import psycopg
from psycopg.rows import dict_row


@lru_cache
def session():
    result = boto3.Session(region_name=os.environ['AWS_REGION'])
    credentials = result.get_credentials()
    # Impide ejecutar el backend con claves de usuario o perfiles locales.
    if credentials is None or credentials.method != 'iam-role':
        raise RuntimeError('El backend requiere el instance profile de EC2 (IAM role).')
    return result


@lru_cache
def s3():
    return session().client('s3')


def bucket():
    return os.environ['S3_BUCKET']


def connect():
    # Recuperación en tiempo de ejecución en cada conexión: sin contraseña en env.
    response = session().client('secretsmanager').get_secret_value(
        SecretId=os.environ['RDS_SECRET_ID'])
    secret = json.loads(response['SecretString'])
    return psycopg.connect(
        host=secret['host'], port=int(secret.get('port', 5432)),
        dbname=secret['dbname'], user=secret['username'], password=secret['password'],
        sslmode='require', connect_timeout=10, row_factory=dict_row,
    )


def initialize():
    with connect() as conn:
        conn.execute(Path(__file__).with_name('schema.sql').read_text())


def delete_objects(keys):
    # DeleteObjects admite hasta 1000 claves; sus errores pueden venir en un HTTP 200.
    for start in range(0, len(keys), 1000):
        result = s3().delete_objects(Bucket=bucket(), Delete={
            'Objects': [{'Key': key} for key in keys[start:start + 1000]],
            'Quiet': True,
        })
        if result.get('Errors'):
            raise RuntimeError('S3 no pudo eliminar todos los objetos solicitados.')
