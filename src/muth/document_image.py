"""Bounded document isolation before OCR and document image measurements.

This stage detects the photographed card, rather than a particular identity or
document field. It never logs pixels, coordinates or OCR text. An uncertain
geometry keeps the complete image; it does not guess a central document crop.
"""

import math
from dataclasses import dataclass, field, replace
from io import BytesIO
from itertools import combinations

from PIL import Image, ImageOps

from muth.media import ImageInput

GEOMETRY_VERSION = "muth-document-geometry-v1"
MAX_INPUT_PIXELS = 32_000_000
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_PIXELS = 3_000_000
_DETECTION_EDGE = 1200
_OUTPUT_EDGE = 2400


@dataclass(frozen=True)
class PreparedDocument:
    image: Image.Image = field(repr=False)
    source_isolated: bool
    strategy: str
    reasons: tuple[str, ...]
    native_size: tuple[int, int]
    rotation_degrees: int = 0
    version: str = GEOMETRY_VERSION
    corners: tuple[tuple[float, float], ...] = ()
    frame_preserved: bool = False

    @property
    def rectified(self) -> bool:
        return self.source_isolated

    @property
    def normalized_corners(self) -> tuple[tuple[float, float], ...]:
        """Clockwise corners in the original EXIF-oriented frame, starting TL."""
        return self.corners

    @property
    def normalized_bounds(self) -> dict[str, float] | None:
        if len(self.corners) == 4:
            x_values, y_values = zip(*self.corners, strict=True)
            minimum_x, minimum_y = min(x_values), min(y_values)
            return {
                "x": minimum_x,
                "y": minimum_y,
                "width": max(x_values) - minimum_x,
                "height": max(y_values) - minimum_y,
            }
        if self.frame_preserved:
            # A retained document-like frame gives no evidence of four visible
            # physical corners. The live capture gate must still require a quad.
            return {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        return None

    def as_image_input(self) -> ImageInput:
        """Encode an internal view without EXIF; pixels are never upscaled."""
        buffer = BytesIO()
        self.image.save(buffer, format="PNG")
        return ImageInput(buffer.getvalue(), *self.image.size, "PNG")


def _vision_modules():
    try:
        import cv2
        import numpy
    except (ImportError, OSError):
        return None
    return cv2, numpy


def geometry_available() -> bool:
    return _vision_modules() is not None


def _contains_transparency(original: Image.Image) -> bool:
    bands = original.getbands()
    if "A" in bands:
        return original.getchannel("A").getextrema()[0] < 255
    if "a" in bands:
        return original.getchannel("a").getextrema()[0] < 255
    if "transparency" in original.info:
        return original.convert("RGBA").getchannel("A").getextrema()[0] < 255
    return False


def _source_copy(original: Image.Image, *, composite_transparency=False) -> Image.Image:
    image = ImageOps.exif_transpose(original)
    if composite_transparency:
        image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image.convert("RGBA"))
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.info.clear()
    return image


def _bounded_image(image: Image.Image) -> Image.Image:
    factor = min(
        1.0,
        _OUTPUT_EDGE / max(image.size),
        math.sqrt(MAX_OUTPUT_PIXELS / (image.width * image.height)),
    )
    if factor < 1:
        image = image.resize(
            (max(1, math.floor(image.width * factor)), max(1, math.floor(image.height * factor))),
            Image.Resampling.LANCZOS,
        )
    return image


def _preserve_card_frame(pixels, np) -> bool:
    """Avoid mistaking a portrait panel within a full document for the card.

    A landscape image whose outer border is already a fairly consistent light
    surface is kept complete. This abstains from a crop; it does not claim that
    such an image is an authenticated or isolated document.
    """
    height, width = pixels.shape[:2]
    if not 1.20 <= width / height <= 2.25:
        return False
    colours = []
    patch = max(2, round(min(width, height) * 0.02))
    for proportion in (0.12, 0.36, 0.64, 0.88):
        x, y = round(width * proportion), round(height * proportion)
        colours.extend(
            [
                pixels[:patch, max(0, x - patch) : x + patch].mean(axis=(0, 1)),
                pixels[-patch:, max(0, x - patch) : x + patch].mean(axis=(0, 1)),
                pixels[max(0, y - patch) : y + patch, :patch].mean(axis=(0, 1)),
                pixels[max(0, y - patch) : y + patch, -patch:].mean(axis=(0, 1)),
            ]
        )
    colours = np.asarray(colours)
    luma = colours @ np.asarray([0.299, 0.587, 0.114])
    return float(np.median(luma)) >= 100 and float(colours.std(axis=0).max()) <= 32


def _order_quad(points, np):
    points = np.asarray(points, dtype="float32").reshape(4, 2)
    centre = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0])
    ordered = points[np.argsort(angles)]
    return np.roll(ordered, -int(np.argmin(ordered.sum(axis=1))), axis=0)


def _quad_dimensions(quad, np):
    lengths = np.linalg.norm(np.roll(quad, -1, axis=0) - quad, axis=1)
    return float((lengths[0] + lengths[2]) / 2), float((lengths[1] + lengths[3]) / 2)


def _valid_quad(quad, width, height, cv2, np):
    # Cropping a screen, keyboard or arbitrary text block should not be easier
    # than keeping the original scene. Four plausible card borders are required.
    if not cv2.isContourConvex(quad.astype("int32")):
        return False
    if (
        np.any(quad[:, 0] < width * 0.007)
        or np.any(quad[:, 0] > width * 0.993)
        or np.any(quad[:, 1] < height * 0.007)
        or np.any(quad[:, 1] > height * 0.993)
    ):
        return False
    area_fraction = cv2.contourArea(quad.astype("float32")) / (width * height)
    if not 0.10 <= area_fraction <= 0.90:
        return False
    vectors = np.roll(quad, -1, axis=0) - quad
    lengths = np.linalg.norm(vectors, axis=1)
    if min(lengths) < min(width, height) * 0.18:
        return False
    units = vectors / lengths[:, None]
    if any(abs(float(np.dot(units[index], units[(index + 1) % 4]))) > 0.36 for index in range(4)):
        return False
    if any(abs(float(np.dot(units[index], units[index + 2]))) < 0.94 for index in range(2)):
        return False
    if max(lengths[0], lengths[2]) / min(lengths[0], lengths[2]) > 1.55:
        return False
    if max(lengths[1], lengths[3]) / min(lengths[1], lengths[3]) > 1.55:
        return False
    card_width, card_height = _quad_dimensions(quad, np)
    return 1.20 <= max(card_width, card_height) / min(card_width, card_height) <= 2.25


def _border_contrasts(quad, image, np):
    contrasts = []
    height, width = image.shape[:2]
    distance = max(3, min(width, height) * 0.009)
    for index in range(4):
        start, end = quad[index], quad[(index + 1) % 4]
        vector = end - start
        normal = np.asarray([-vector[1], vector[0]]) / np.linalg.norm(vector)
        points = start[None, :] + np.linspace(0.08, 0.92, 35)[:, None] * vector[None, :]
        colours = []
        for offset in (-distance, distance):
            positions = np.rint(points + offset * normal).astype("int32")
            positions[:, 0] = np.clip(positions[:, 0], 0, width - 1)
            positions[:, 1] = np.clip(positions[:, 1], 0, height - 1)
            colours.append(image[positions[:, 1], positions[:, 0]].astype("float32"))
        contrasts.append(float(np.linalg.norm((colours[0] - colours[1]).mean(axis=0))))
    return contrasts


def _quad_score(quad, blurred, gray, cv2, np, supports=None):
    height, width = gray.shape
    if not _valid_quad(quad, width, height, cv2, np):
        return None
    contrasts = _border_contrasts(quad, blurred, np)
    if min(contrasts) < 12 or sum(contrasts) / 4 < 27:
        return None
    # Sample a bounded grid, rather than allocate a full-size mask per candidate.
    centre = quad.mean(axis=0)
    samples = centre + (quad - centre) * 0.65
    positions = np.rint(np.concatenate([samples, centre[None, :]])).astype("int32")
    if float(gray[positions[:, 1], positions[:, 0]].mean()) < 75:
        return None
    if supports is not None and (min(supports) < 0.30 or sum(supports) / 4 < 0.55):
        return None
    card_width, card_height = _quad_dimensions(quad, np)
    ratio = max(card_width, card_height) / min(card_width, card_height)
    centrality = float(np.linalg.norm(centre / np.asarray([width, height]) - 0.5))
    support_score = sum(supports) / 4 if supports is not None else 0.8
    return sum(contrasts) / 480 + support_score * 0.5 - abs(ratio - 1.60) * 0.1 - centrality * 0.2


def _contour_quad(blurred, gray, cv2, np):
    height, width = gray.shape
    edges = cv2.Canny(gray, 35, 110)
    masks = [
        edges,
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), dtype="uint8")),
    ]
    masks.extend(
        cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)[1] for threshold in (110, 150, 190)
    )
    best = None
    for mask in masks:
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:32]
        for contour in contours:
            if cv2.contourArea(contour) < width * height * 0.10:
                continue
            perimeter = cv2.arcLength(contour, True)
            for epsilon in (0.015, 0.025, 0.035, 0.05):
                polygon = cv2.approxPolyDP(contour, epsilon * perimeter, True)
                if len(polygon) != 4:
                    continue
                quad = _order_quad(polygon, np)
                score = _quad_score(quad, blurred, gray, cv2, np)
                if score is not None and (best is None or score > best[0]):
                    best = score, quad
                break
    return best[1] if best is not None else None


def _line_quad(blurred, gray, cv2, np, *, thresholds=(20, 60)):
    """Recover broken borders without using OCR text or a fixed central crop."""
    height, width = gray.shape
    minimum = min(width, height)
    lines = cv2.HoughLinesP(
        cv2.Canny(gray, *thresholds),
        1,
        np.pi / 360,
        max(55, round(minimum * 0.10)),
        minLineLength=max(80, round(minimum * 0.19)),
        maxLineGap=max(10, round(minimum * 0.035)),
    )
    if lines is None:
        return None
    horizontal, vertical = [], []
    for line in lines[:512]:
        start, end = (
            np.asarray(line[0][:2], dtype="float32"),
            np.asarray(line[0][2:], dtype="float32"),
        )
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length == 0:
            continue
        angle = math.degrees(math.atan2(float(vector[1]), float(vector[0]))) % 180
        if min(angle, 180 - angle) < 25:
            group = horizontal
        elif abs(angle - 90) < 25:
            group = vertical
        else:
            continue
        normal = np.asarray([-vector[1], vector[0]]) / length
        dominant = 0 if abs(normal[0]) > abs(normal[1]) else 1
        if normal[dominant] < 0:
            normal *= -1
        equation = np.asarray([normal[0], normal[1], -float(np.dot(normal, start))])
        if any(
            abs(equation[2] - previous[0][2]) < minimum * 0.008
            and abs(float(np.dot(equation[:2], previous[0][:2]))) > 0.999
            for previous in group
        ):
            continue
        group.append((equation, start, end, length))
    horizontal = sorted(horizontal, key=lambda item: item[3], reverse=True)[:20]
    vertical = sorted(vertical, key=lambda item: item[3], reverse=True)[:12]
    best = None
    for first_h, second_h in combinations(horizontal, 2):
        if abs(float(np.dot(first_h[0][:2], second_h[0][:2]))) < 0.975:
            continue
        for first_v, second_v in combinations(vertical, 2):
            if abs(float(np.dot(first_v[0][:2], second_v[0][:2]))) < 0.975:
                continue
            intersections = []
            for h_line, v_line in (
                (first_h, first_v),
                (first_h, second_v),
                (second_h, second_v),
                (second_h, first_v),
            ):
                coefficients = np.asarray([h_line[0][:2], v_line[0][:2]])
                if abs(float(np.linalg.det(coefficients))) < 0.94:
                    break
                intersections.append(
                    np.linalg.solve(coefficients, -np.asarray([h_line[0][2], v_line[0][2]]))
                )
            if len(intersections) != 4:
                continue
            # Keep corresponding lines until support has been measured, then
            # order for perspective correction. Shape checks do not need TL.
            unordered = np.asarray(intersections, dtype="float32")
            supports = []
            for index, border in enumerate((first_h, second_v, second_h, first_v)):
                start, end = unordered[index], unordered[(index + 1) % 4]
                vector = end - start
                squared_length = float(np.dot(vector, vector))
                if squared_length == 0:
                    supports.append(0.0)
                    continue
                positions = [
                    float(np.dot(point - start, vector)) / squared_length for point in border[1:3]
                ]
                supports.append(max(0.0, min(1.0, max(positions)) - max(0.0, min(positions))))
            score = _quad_score(unordered, blurred, gray, cv2, np, supports)
            if score is not None and (best is None or score > best[0]):
                best = score, _order_quad(unordered, np)
    return best[1] if best is not None else None


def prepare_document_image(original: Image.Image) -> PreparedDocument:
    if (
        original.width < 1
        or original.height < 1
        or original.width * original.height > MAX_INPUT_PIXELS
    ):
        raise ValueError("document_image_pixel_limit")
    transparent = _contains_transparency(original)
    image = _source_copy(original, composite_transparency=transparent)
    fallback_image = _bounded_image(image)
    if transparent:
        return PreparedDocument(
            image=fallback_image,
            source_isolated=False,
            strategy="original",
            reasons=("document_transparency_unsupported",),
            native_size=image.size,
        )
    fallback = PreparedDocument(
        image=fallback_image,
        source_isolated=False,
        strategy="original",
        reasons=("document_geometry_uncertain",),
        native_size=image.size,
    )
    modules = _vision_modules()
    if modules is None:
        return PreparedDocument(
            image=fallback_image,
            source_isolated=False,
            strategy="original",
            reasons=("document_geometry_unavailable",),
            native_size=image.size,
        )
    cv2, np = modules
    scale = min(1.0, _DETECTION_EDGE / max(image.size))
    working = (
        image
        if scale == 1
        else image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    )
    pixels = np.asarray(working)
    if _preserve_card_frame(pixels, np):
        return replace(fallback, frame_preserved=True)
    blurred = cv2.GaussianBlur(pixels, (11, 11), 0)
    gray = cv2.cvtColor(blurred, cv2.COLOR_RGB2GRAY)
    contour_gray = cv2.cvtColor(cv2.GaussianBlur(pixels, (5, 5), 0), cv2.COLOR_RGB2GRAY)
    quad = _contour_quad(blurred, contour_gray, cv2, np)
    strategy = "quadrilateral"
    if quad is None:
        quad = _line_quad(blurred, gray, cv2, np)
        strategy = "supported_borders"
    if quad is None:
        quad = _line_quad(blurred, gray, cv2, np, thresholds=(15, 45))
    if quad is None:
        return fallback
    # Overlay metadata uses the actual rounded detection-frame dimensions.
    # Keep the validated isotropic mapping for rectification: changing the
    # sampling transform by even a fraction of a pixel can change OCR results.
    corners = tuple((float(x) / working.width, float(y) / working.height) for x, y in quad)
    quad = quad / scale
    target_width = max(
        1, round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
    )
    target_height = max(
        1, round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
    )
    native_size = target_width, target_height
    output_factor = min(
        1.0,
        _OUTPUT_EDGE / max(native_size),
        math.sqrt(MAX_OUTPUT_PIXELS / (target_width * target_height)),
    )
    target_width = max(1, math.floor(target_width * output_factor))
    target_height = max(1, math.floor(target_height * output_factor))
    destination = np.asarray(
        [
            [0, 0],
            [target_width - 1, 0],
            [target_width - 1, target_height - 1],
            [0, target_height - 1],
        ],
        dtype="float32",
    )
    transform = cv2.getPerspectiveTransform(quad.astype("float32"), destination)
    rectified = Image.fromarray(
        cv2.warpPerspective(
            np.asarray(image),
            transform,
            (target_width, target_height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    )
    rotation = 0
    if rectified.height > rectified.width:
        rectified = rectified.transpose(Image.Transpose.ROTATE_270)
        native_size = native_size[1], native_size[0]
        rotation = 90
    return PreparedDocument(
        image=rectified,
        source_isolated=True,
        strategy=strategy,
        reasons=("document_region_isolated", "document_perspective_corrected"),
        native_size=native_size,
        rotation_degrees=rotation,
        corners=corners,
    )


def prepare_document(image: ImageInput) -> PreparedDocument:
    if len(image.content) > MAX_INPUT_BYTES:
        raise ValueError("document_image_byte_limit")
    with Image.open(BytesIO(image.content)) as original:
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError("document_image_multiple_frames")
        return prepare_document_image(original)
