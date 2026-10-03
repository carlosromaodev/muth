import unittest
from io import BytesIO
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from muth.document_image import (
    MAX_INPUT_BYTES,
    MAX_INPUT_PIXELS,
    MAX_OUTPUT_PIXELS,
    geometry_available,
    prepare_document,
    prepare_document_image,
)
from muth.media import ImageInput


def synthetic_card_scene(*, occluded=False):
    import cv2

    background = Image.new("RGB", (1200, 1000), (45, 75, 104))
    draw = ImageDraw.Draw(background)
    draw.rectangle((20, 100, 410, 850), fill=(10, 21, 42), outline=(65, 90, 105), width=8)
    for index in range(25):
        draw.text((40, 130 + index * 25), "BACKGROUND SCREEN - SYNTHETIC", fill=(100, 190, 244))
    card = Image.new("RGB", (700, 440), (218, 207, 168))
    draw = ImageDraw.Draw(card)
    draw.text((35, 35), "DOCUMENTO SINTETICO", fill=(20, 20, 20))
    draw.text((35, 115), "NOME: PESSOA SINTETICA", fill=(20, 20, 20))
    draw.text((35, 195), "NAO E UM DOCUMENTO REAL", fill=(20, 20, 20))
    draw.rectangle((505, 100, 655, 330), fill=(142, 132, 103))
    source = np.asarray([[0, 0], [699, 0], [699, 439], [0, 439]], dtype="float32")
    destination = np.asarray([[405, 300], [1100, 350], [1060, 775], [370, 720]], dtype="float32")
    transform = cv2.getPerspectiveTransform(source, destination)
    warped = cv2.warpPerspective(np.asarray(card), transform, background.size)
    mask = cv2.warpPerspective(np.full((440, 700), 255, dtype="uint8"), transform, background.size)
    background = Image.composite(Image.fromarray(warped), background, Image.fromarray(mask))
    if occluded:
        draw = ImageDraw.Draw(background)
        draw.ellipse((600, 230, 790, 335), fill=(145, 73, 42))
        draw.ellipse((670, 750, 840, 840), fill=(145, 73, 42))
    return background


@unittest.skipUnless(geometry_available(), "Document geometry requires OpenCV and numpy")
class DocumentGeometryTests(unittest.TestCase):
    def assert_document_without_screen(self, result):
        self.assertTrue(result.source_isolated)
        self.assertGreater(result.image.width, result.image.height)
        self.assertLess(result.image.width, 780)
        self.assertLess(result.image.height, 510)
        pixels = np.asarray(result.image).astype("int16")
        blue = (pixels[:, :, 2] - pixels[:, :, 0] > 35) & (pixels[:, :, 2] - pixels[:, :, 1] > 15)
        self.assertLess(float(blue.mean()), 0.025)
        self.assertIn("document_region_isolated", result.reasons)

    def test_isolates_perspective_card_from_screen_text(self):
        original = synthetic_card_scene()
        original_bytes = original.tobytes()
        result = prepare_document_image(original)
        self.assert_document_without_screen(result)
        self.assertEqual(original.tobytes(), original_bytes)
        self.assertEqual(result.native_size, result.image.size)

    def test_overlay_corners_and_bounds_refer_to_the_source_scene(self):
        result = prepare_document_image(synthetic_card_scene())
        expected = (
            (405 / 1200, 300 / 1000),
            (1100 / 1200, 350 / 1000),
            (1060 / 1200, 775 / 1000),
            (370 / 1200, 720 / 1000),
        )
        self.assertEqual(len(result.corners), 4)
        self.assertEqual(result.normalized_corners, result.corners)
        for actual, target in zip(result.corners, expected, strict=True):
            self.assertAlmostEqual(actual[0], target[0], delta=0.015)
            self.assertAlmostEqual(actual[1], target[1], delta=0.015)
            self.assertTrue(all(0 < coordinate < 1 for coordinate in actual))
        bounds = result.normalized_bounds
        self.assertAlmostEqual(bounds["x"], 370 / 1200, delta=0.015)
        self.assertAlmostEqual(bounds["y"], 300 / 1000, delta=0.015)
        self.assertAlmostEqual(bounds["width"], (1100 - 370) / 1200, delta=0.025)
        self.assertAlmostEqual(bounds["height"], (775 - 300) / 1000, delta=0.025)
        self.assertFalse(result.frame_preserved)

    def test_overlay_coordinates_follow_exif_orientation(self):
        source = synthetic_card_scene()
        exif = Image.Exif()
        exif[274] = 6
        buffer = BytesIO()
        source.save(buffer, format="PNG", exif=exif)
        prepared = prepare_document(ImageInput(buffer.getvalue(), *source.size, "PNG"))
        oriented = prepare_document_image(source.transpose(Image.Transpose.ROTATE_270))
        self.assertTrue(prepared.source_isolated)
        self.assertEqual(prepared.corners, oriented.corners)
        self.assertEqual(prepared.normalized_bounds, oriented.normalized_bounds)
        self.assertEqual(prepared.native_size, oriented.native_size)

    def test_normalized_overlay_does_not_depend_on_source_resolution(self):
        source = synthetic_card_scene()
        first = prepare_document_image(source)
        enlarged = prepare_document_image(source.resize((3600, 3000), Image.Resampling.LANCZOS))
        self.assertTrue(enlarged.source_isolated)
        for actual, target in zip(enlarged.corners, first.corners, strict=True):
            self.assertAlmostEqual(actual[0], target[0], delta=0.015)
            self.assertAlmostEqual(actual[1], target[1], delta=0.015)
        self.assertGreater(enlarged.native_size[0], first.native_size[0] * 2.8)
        self.assertLessEqual(max(enlarged.image.size), 2400)
        self.assertLessEqual(enlarged.image.width * enlarged.image.height, MAX_OUTPUT_PIXELS)

    def test_overlay_rounding_does_not_change_validated_rectification_sampling(self):
        import cv2

        source = Image.new("RGB", (1601, 1207), (190, 180, 160))
        detected = np.asarray([[190, 140], [1000, 190], [990, 690], [200, 650]], dtype="float32")
        with (
            patch("muth.document_image._preserve_card_frame", return_value=False),
            patch("muth.document_image._contour_quad", return_value=detected),
            patch.object(cv2, "getPerspectiveTransform", wraps=cv2.getPerspectiveTransform) as warp,
        ):
            result = prepare_document_image(source)

        # This source rounds to a 1200 × 905 detection frame. Its overlay must
        # use those real dimensions while the warp keeps the validated uniform
        # source scale, including the fractional y coordinate.
        self.assertTrue(result.source_isolated)
        self.assertEqual(result.corners[0], (190 / 1200, 140 / 905))
        sampled_source = warp.call_args.args[0]
        self.assertAlmostEqual(float(sampled_source[0, 0]), 190 * 1601 / 1200, places=4)
        self.assertAlmostEqual(float(sampled_source[0, 1]), 140 * 1601 / 1200, places=4)
        self.assertNotAlmostEqual(float(sampled_source[0, 1]), 140 * 1207 / 905, places=2)

    def test_partial_finger_occlusion_keeps_the_card_not_background(self):
        result = prepare_document_image(synthetic_card_scene(occluded=True))
        self.assert_document_without_screen(result)

    def test_rotated_card_is_rectified_to_landscape(self):
        rotated = synthetic_card_scene().transpose(Image.Transpose.ROTATE_90)
        result = prepare_document_image(rotated)
        self.assert_document_without_screen(result)
        self.assertEqual(result.rotation_degrees, 90)

    def test_card_with_broken_borders_is_isolated_at_all_quarter_turns(self):
        scene = synthetic_card_scene(occluded=True)
        for angle in (0, 90, 180, 270):
            with self.subTest(angle=angle):
                result = prepare_document_image(scene.rotate(angle, expand=True))
                self.assert_document_without_screen(result)

    def test_second_pass_keeps_the_complete_rectified_document(self):
        first = prepare_document_image(synthetic_card_scene())
        second = prepare_document_image(first.image)
        self.assertEqual(second.image.size, first.image.size)
        self.assertEqual(second.image.tobytes(), first.image.tobytes())

    def test_full_document_with_portrait_panel_is_not_replaced_by_panel(self):
        full_document = Image.new("RGB", (1800, 1100), "white")
        draw = ImageDraw.Draw(full_document)
        draw.text((60, 80), "DOCUMENTO SINTETICO", fill="black")
        draw.rectangle((1280, 320, 1755, 1020), fill=(90, 110, 130))
        for position in range(360, 990, 24):
            draw.rectangle((1310, position, 1715, position + 9), fill=(160, 160, 160))
        result = prepare_document_image(full_document)
        self.assertFalse(result.source_isolated)
        self.assertEqual(result.image.size, full_document.size)
        self.assertEqual(result.image.tobytes(), full_document.tobytes())
        self.assertEqual(result.corners, ())
        self.assertTrue(result.frame_preserved)
        self.assertEqual(
            result.normalized_bounds, {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        )

    def test_detection_downsample_does_not_reduce_native_document_pixels(self):
        larger_scene = synthetic_card_scene().resize((2400, 2000), Image.Resampling.LANCZOS)
        result = prepare_document_image(larger_scene)
        self.assertTrue(result.source_isolated)
        self.assertGreater(result.image.width, 1300)
        self.assertGreater(result.image.height, 800)
        self.assertEqual(result.native_size, result.image.size)

    def test_blank_image_is_not_assumed_to_be_a_document(self):
        result = prepare_document_image(Image.new("RGB", (900, 650), "white"))
        self.assertFalse(result.source_isolated)
        self.assertEqual(result.strategy, "original")
        self.assertEqual(result.image.size, (900, 650))
        self.assertEqual(result.reasons, ("document_geometry_uncertain",))

    def test_grayscale_source_and_image_input_are_supported(self):
        scene = synthetic_card_scene().convert("L")
        buffer = BytesIO()
        scene.save(buffer, format="PNG")
        result = prepare_document(ImageInput(buffer.getvalue(), *scene.size, "PNG"))
        self.assertTrue(result.source_isolated)
        encoded = result.as_image_input()
        self.assertEqual((encoded.width, encoded.height), result.image.size)
        with Image.open(BytesIO(encoded.content)) as decoded:
            self.assertEqual(decoded.mode, "RGB")
            self.assertFalse(decoded.getexif())


class DocumentGeometrySafetyTests(unittest.TestCase):
    def assert_transparency_is_not_processed(self, image):
        with patch("muth.document_image._vision_modules") as modules:
            result = prepare_document_image(image)
        modules.assert_not_called()
        self.assertFalse(result.source_isolated)
        self.assertEqual(result.reasons, ("document_transparency_unsupported",))
        self.assertEqual(result.image.mode, "RGB")
        self.assertEqual(result.corners, ())
        self.assertIsNone(result.normalized_bounds)
        return result

    def test_invisible_rgba_scene_does_not_reveal_hidden_document_pixels(self):
        hidden = Image.new("RGBA", (90, 70), (9, 30, 50, 0))
        result = self.assert_transparency_is_not_processed(hidden)
        self.assertEqual(result.image.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_any_nonopaque_pixel_is_rejected_before_card_detection(self):
        image = Image.new("RGBA", (90, 70), (180, 180, 180, 255))
        image.putpixel((30, 20), (0, 0, 0, 254))
        result = self.assert_transparency_is_not_processed(image)
        self.assertEqual(result.image.getpixel((30, 20)), (1, 1, 1))

    def test_transparent_grayscale_is_composited_on_white(self):
        result = self.assert_transparency_is_not_processed(Image.new("LA", (90, 70), (0, 0)))
        self.assertEqual(result.image.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_transparent_palette_is_composited_on_white(self):
        image = Image.new("P", (90, 70), 0)
        image.putpalette([0, 20, 40, 190, 190, 190] + [0] * 762)
        image.info["transparency"] = 0
        result = self.assert_transparency_is_not_processed(image)
        self.assertEqual(result.image.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_rgb_transparent_colour_header_does_not_preserve_hidden_pixels(self):
        image = Image.new("RGB", (90, 70), (1, 20, 30))
        image.info["transparency"] = (1, 20, 30)
        result = self.assert_transparency_is_not_processed(image)
        self.assertEqual(result.image.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_opaque_alpha_and_unused_transparent_palette_entry_are_allowed(self):
        rgba = Image.new("RGBA", (90, 70), (190, 190, 190, 255))
        grayscale = Image.new("LA", (90, 70), (190, 255))
        palette = Image.new("P", (90, 70), 1)
        palette.putpalette([0, 0, 0, 190, 190, 190] + [0] * 762)
        palette.info["transparency"] = 0
        for image in (rgba, grayscale, palette):
            with self.subTest(mode=image.mode):
                result = prepare_document_image(image)
                self.assertNotIn("document_transparency_unsupported", result.reasons)
                self.assertEqual(result.image.getextrema(), ((190, 190), (190, 190), (190, 190)))

    def test_missing_geometry_dependency_keeps_original_image(self):
        original = Image.new("RGB", (480, 320), (210, 204, 168))
        with patch("muth.document_image._vision_modules", return_value=None):
            result = prepare_document_image(original)
            self.assertFalse(geometry_available())
        self.assertFalse(result.source_isolated)
        self.assertEqual(result.image.tobytes(), original.tobytes())
        self.assertEqual(result.reasons, ("document_geometry_unavailable",))

    def test_exif_orientation_is_applied_and_private_metadata_is_removed(self):
        original = Image.new("RGB", (120, 80), "white")
        ImageDraw.Draw(original).rectangle((0, 0, 20, 20), fill="black")
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "SYNTHETIC PRIVATE METADATA"
        buffer = BytesIO()
        original.save(buffer, format="JPEG", exif=exif)
        with patch("muth.document_image._vision_modules", return_value=None):
            result = prepare_document(ImageInput(buffer.getvalue(), 120, 80, "JPEG"))
        self.assertEqual(result.image.size, (80, 120))
        self.assertFalse(result.image.info)
        with Image.open(BytesIO(result.as_image_input().content)) as decoded:
            self.assertFalse(decoded.getexif())

    def test_output_is_bounded_without_upscaling(self):
        with patch("muth.document_image._vision_modules", return_value=None):
            result = prepare_document_image(Image.new("RGB", (4000, 2500), "white"))
        self.assertLessEqual(max(result.image.size), 2400)
        self.assertLessEqual(result.image.width * result.image.height, MAX_OUTPUT_PIXELS)
        self.assertEqual(prepare_document_image(Image.new("RGB", (40, 30))).image.size, (40, 30))

    def test_pixel_limit_is_checked_before_loading_pixels(self):
        class Oversized:
            width = MAX_INPUT_PIXELS
            height = 2

            def load(self):
                raise AssertionError("Oversized input must not decode")

        with self.assertRaisesRegex(ValueError, "document_image_pixel_limit"):
            prepare_document_image(Oversized())

    def test_byte_limit_is_checked_before_decoding(self):
        with self.assertRaisesRegex(ValueError, "document_image_byte_limit"):
            prepare_document(ImageInput(b"x" * (MAX_INPUT_BYTES + 1), 1, 1, "PNG"))

    def test_animated_input_is_rejected(self):
        buffer = BytesIO()
        Image.new("RGB", (10, 10), "white").save(
            buffer, format="GIF", save_all=True, append_images=[Image.new("RGB", (10, 10), "black")]
        )
        with self.assertRaisesRegex(ValueError, "document_image_multiple_frames"):
            prepare_document(ImageInput(buffer.getvalue(), 10, 10, "GIF"))


if __name__ == "__main__":
    unittest.main()
