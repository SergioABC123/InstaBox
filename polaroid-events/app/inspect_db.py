"""Ejecutar EN EC2: python -m app.inspect_db. No imprime credenciales."""
from .cloud import connect

with connect() as conn:
    for table in ('events', 'photos'):
        print(f'\n--- {table} ---')
        # Los identificadores son constantes del código, no entrada de usuario.
        for row in conn.execute(f'SELECT * FROM {table} ORDER BY created_at').fetchall():
            print(row)
