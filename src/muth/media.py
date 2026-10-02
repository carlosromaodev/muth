from dataclasses import dataclass, field
from io import BytesIO

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from muth.config import Settings


@dataclass(frozen=True)
class ImageInput:
    content: bytes = field(repr=False)
    width: int
    height: int
    format: str


async def read_image(upload: UploadFile, settings: Settings) -> ImageInput:
    try:
        content = await upload.read(settings.max_upload_bytes + 1)
    finally:
        await upload.close()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "Imagem excede o limite permitido.")
    if not content:
        raise HTTPException(422, "A imagem está vazia.")
    return await run_in_threadpool(decode_image, content, settings)


def decode_image(content: bytes, settings: Settings) -> ImageInput:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"JPEG", "PNG"}:
                raise HTTPException(415, "Apenas imagens JPEG ou PNG são aceites.")
            if image.width * image.height > settings.max_image_pixels:
                raise HTTPException(413, "A resolução da imagem excede o limite permitido.")
            if getattr(image, "n_frames", 1) != 1:
                raise HTTPException(415, "Imagens animadas não são aceites.")
            result = ImageInput(content, image.width, image.height, image.format)
            image.verify()
        # Decode as well: structural verification alone can accept truncated JPEGs.
        with Image.open(BytesIO(content)) as image:
            image.load()
        return result
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(413, "A resolução da imagem excede o limite permitido.") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise HTTPException(422, "Ficheiro de imagem inválido ou corrompido.") from exc
