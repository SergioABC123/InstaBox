"""Composición sin persistir la foto original de alta resolución."""
import io
import os
import warnings

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 24_000_000
MAX_BYTES = 10 * 1024 * 1024


def render_photo(data: bytes, message: str):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('La foto debe pesar entre 1 byte y 10 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise ValueError('Usa una foto JPEG, PNG o WEBP.')
                photo = ImageOps.exif_transpose(source).convert('RGBA')
                background = Image.new('RGBA', photo.size, 'white')
                background.alpha_composite(photo)
                photo = background.convert('RGB')
                reduced = ImageOps.fit(photo, (128, 128), method=Image.Resampling.LANCZOS)
                # La polaroid usa la entrada original para no ampliar una miniatura.
                large = ImageOps.fit(photo, (900, 900), method=Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError('Imagen dañada, inválida o demasiado grande.') from exc

    font_path = os.getenv('FONT_PATH', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    font = ImageFont.truetype(font_path, 30)
    canvas = Image.new('RGB', (1020, 1320), 'white')
    canvas.paste(large, (60, 60))
    draw = ImageDraw.Draw(canvas)
    # División por ancho real, también para palabras largas. Máximo 300 caracteres.
    lines, current = [], ''
    for char in ' '.join(message.split()):
        if draw.textlength(current + char, font=font) > 900:
            lines.append(current.rstrip())
            current = char.lstrip()
        else:
            current += char
    if current:
        lines.append(current.rstrip())
    if len(lines) > 8:
        raise ValueError('El mensaje no cabe; usa un mensaje más corto.')
    for index, line in enumerate(lines):
        draw.text((60, 992 + index * 36), line, font=font, fill='#242424')
    original_out, polaroid_out = io.BytesIO(), io.BytesIO()
    reduced.save(original_out, format='JPEG', quality=90)
    canvas.save(polaroid_out, format='JPEG', quality=95, dpi=(300, 300))
    return original_out.getvalue(), polaroid_out.getvalue()
