import os
import sys
import shutil
import struct
import unittest
from pathlib import Path
from datetime import datetime

import rename_images


def create_mock_jpeg(file_path: Path, camera: str = "Nikon D850", date_str: str = "2024:08:15 14:30:00") -> None:
    endian = "<"
    tiff_header = b"II\x2a\x00\x08\x00\x00\x00"

    model_bytes = camera.encode("ascii") + b"\x00"
    exif_subifd_offset = 38 + len(model_bytes)
    date_bytes = date_str.encode("ascii") + b"\x00"
    date_offset = exif_subifd_offset + 2 + 12 + 4

    ifd0 = struct.pack(f"{endian}H", 2)
    ifd0 += struct.pack(f"{endian}HHI", 0x0110, 2, len(model_bytes)) + struct.pack(f"{endian}I", 38)
    ifd0 += struct.pack(f"{endian}HHI", 0x8769, 4, 1) + struct.pack(f"{endian}I", exif_subifd_offset)
    ifd0 += b"\x00\x00\x00\x00"

    subifd = struct.pack(f"{endian}H", 1)
    subifd += struct.pack(f"{endian}HHI", 0x9003, 2, len(date_bytes)) + struct.pack(f"{endian}I", date_offset)
    subifd += b"\x00\x00\x00\x00"

    tiff_payload = tiff_header + ifd0 + model_bytes + subifd + date_bytes
    exif_app1 = b"\xff\xe1" + struct.pack(">H", len(tiff_payload) + 8) + b"Exif\x00\x00" + tiff_payload
    jpeg_bytes = b"\xff\xd8" + exif_app1 + b"\xff\xd9"

    with open(file_path, "wb") as f:
        f.write(jpeg_bytes)


class TestRenameImages(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("test_mock_dir").resolve()
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_exif_extraction(self):
        img = self.test_dir / "sample.jpg"
        create_mock_jpeg(img, camera="Canon EOS R5", date_str="2023:05:10 10:20:30")
        meta = rename_images.get_image_metadata(img, date_format="%Y-%m-%d_%H-%M-%S")
        self.assertEqual(meta["camera"], "Canon_EOS_R5")
        self.assertEqual(meta["date"], "2023-05-10_10-20-30")
        self.assertTrue("B" in meta["size"] or "KB" in meta["size"])

    def test_exif_found_after_xmp_segment(self):
        img = self.test_dir / "xmp_first.jpg"
        create_mock_jpeg(img, camera="Canon EOS R5")
        data = img.read_bytes()
        xmp = b"http://ns.adobe.com/xap/1.0/\x00<x/>"
        xmp_app1 = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
        img.write_bytes(data[:2] + xmp_app1 + data[2:])
        meta = rename_images.get_image_metadata(img)
        self.assertEqual(meta["camera"], "Canon_EOS_R5")

    def test_build_new_filename_flags(self):
        meta = {
            "date": "2024-01-01_12-00-00",
            "camera": "Sony_A7",
            "size": "250KB",
            "orig": "IMG_001"
        }
        name = rename_images.build_new_filename(
            metadata=meta,
            description="blue_mountain_lake",
            include_date=True,
            include_camera=True,
            include_size=True,
            ext=".jpg"
        )
        self.assertEqual(name, "2024-01-01_12-00-00_Sony_A7_blue_mountain_lake_250KB.jpg")

    def test_build_new_filename_pattern(self):
        meta = {
            "date": "2024-01-01",
            "camera": "Sony_A7",
            "size": "250KB",
            "orig": "IMG_001"
        }
        pattern = "{date}_{desc}_{size}"
        name = rename_images.build_new_filename(
            metadata=meta,
            description="sunny_beach",
            pattern=pattern,
            ext=".png"
        )
        self.assertEqual(name, "2024-01-01_sunny_beach_250KB.png")

    def test_duplicate_collision_handling(self):
        claimed = set()
        img1 = self.test_dir / "img1.jpg"
        img2 = self.test_dir / "img2.jpg"
        img1.touch()
        img2.touch()

        target1 = rename_images.resolve_target_path(self.test_dir, "test_name.jpg", img1, claimed)
        self.assertEqual(target1.name, "test_name.jpg")

        target2 = rename_images.resolve_target_path(self.test_dir, "test_name.jpg", img2, claimed)
        self.assertEqual(target2.name, "test_name_1.jpg")

        target3 = rename_images.resolve_target_path(self.test_dir, "test_name.jpg", self.test_dir / "img3.jpg", claimed)
        self.assertEqual(target3.name, "test_name_2.jpg")

    def test_dry_run_does_not_modify_files(self):
        img = self.test_dir / "photo.jpg"
        create_mock_jpeg(img)

        # Mock description generation to avoid network call in unit test
        original_gen = rename_images.generate_description
        rename_images.generate_description = lambda *args, **kwargs: "mocked description"
        try:
            results = rename_images.rename_images_in_folder(
                folder_path=self.test_dir,
                dry_run=True,
                include_date=True,
                api_key="mock_key",
                context_length=1000
            )
            self.assertTrue(img.exists())
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "dry_run")
            self.assertNotEqual(results[0]["target"].name, img.name)
        finally:
            rename_images.generate_description = original_gen

    def test_actual_renaming_works(self):
        img = self.test_dir / "photo.jpg"
        create_mock_jpeg(img)

        original_gen = rename_images.generate_description
        rename_images.generate_description = lambda *args, **kwargs: "red sports car"
        try:
            with self.assertLogs("image_renamer", level="INFO") as logs:
                results = rename_images.rename_images_in_folder(
                    folder_path=self.test_dir,
                    dry_run=False,
                    include_date=True,
                    include_camera=True,
                    api_key="mock_key",
                    context_length=1000
                )
            rename_lines = [line for line in logs.output if "renamed: " in line]
            self.assertEqual(len(rename_lines), 1)
            self.assertIn(str(img), rename_lines[0])
            self.assertIn(str(results[0]["target"]), rename_lines[0])
            self.assertTrue(any("Summary: renamed=1" in line for line in logs.output))
            self.assertFalse(img.exists())
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "renamed")
            target_file = results[0]["target"]
            self.assertTrue(target_file.exists())
            self.assertIn("red_sports_car", target_file.name)
            self.assertIn("Nikon_D850", target_file.name)
        finally:
            rename_images.generate_description = original_gen

    def test_skip_already_renamed(self):
        img = self.test_dir / "2024-05-01_10-00-00_already_named.jpg"
        img.touch()

        original_gen = rename_images.generate_description
        rename_images.generate_description = lambda *args, **kwargs: "new description"
        try:
            results = rename_images.rename_images_in_folder(
                folder_path=self.test_dir,
                skip_renamed=True,
                include_date=True,
                api_key="mock_key",
                context_length=1000
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "skipped")
            self.assertTrue(img.exists())
        finally:
            rename_images.generate_description = original_gen


if __name__ == "__main__":
    unittest.main()
