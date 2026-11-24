import base64
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, PngImagePlugin

from harness.artifacts.files import file_name
from harness.artifacts.preview import MAX_IMAGE_BYTES, preview


def _images(blocks):
    return [block for block in blocks if block["type"] == "image"]


class ArtifactPreviewTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_content_addressed_upload_keeps_source_and_produces_bounded_image(self):
        source = self.directory / "blob"
        info = PngImagePlugin.PngInfo()
        info.add_text("private", "source metadata")
        Image.new("RGB", (2048, 256), "red").save(source, "PNG", pnginfo=info)
        original = source.read_bytes()

        [block] = _images(preview(source, filename="reference.png", automatic=True))

        self.assertEqual(block["name"], "reference.png")
        self.assertEqual(block["mime_type"], "image/png")
        data = base64.b64decode(block["data_base64"], validate=True)
        self.assertLessEqual(len(data), MAX_IMAGE_BYTES)
        with Image.open(io.BytesIO(data)) as rendered:
            self.assertEqual(rendered.size, (1536, 192))
            self.assertNotIn("private", rendered.info)
        self.assertEqual(source.read_bytes(), original)

    def test_crop_uses_workspace_image_coordinates(self):
        source = self.directory / "source.png"
        image = Image.new("RGB", (20, 20), "red")
        image.paste((0, 0, 255), (10, 10, 20, 20))
        image.save(source)

        [block] = _images(preview(source, crop=(10, 10, 10, 10)))

        with Image.open(io.BytesIO(base64.b64decode(block["data_base64"]))) as result:
            self.assertEqual(result.size, (10, 10))
            self.assertEqual(result.getpixel((0, 0)), (0, 0, 255))
        with self.assertRaisesRegex(ValueError, "beyond the image"):
            preview(source, crop=(15, 15, 10, 10))

    def test_multibyte_derived_image_names_fit_storage_limits(self):
        source = self.directory / "image.gif"
        Image.new("RGB", (12, 12), "red").save(
            source, save_all=True, append_images=[Image.new("RGB", (12, 12), "blue")], duration=100,
        )

        frames = _images(preview(source, filename=f"{'图' * 78}.gif"))

        self.assertEqual(len(frames), 2)
        for frame in frames:
            self.assertEqual(file_name(frame["name"]), frame["name"])
        self.assertTrue(frames[0]["name"].endswith("-frame-1.png"))
        self.assertTrue(frames[1]["name"].endswith("-frame-2.png"))

    @unittest.skipUnless(shutil.which("pdftoppm"), "pdftoppm is unavailable")
    def test_pdf_pages_respect_image_limit_and_never_repeat_the_previous_output(self):
        source = self.directory / "blob"
        first = Image.new("RGB", (80, 40), "white")
        second = Image.new("RGB", (80, 40), "gray")
        first.save(source, "PDF", save_all=True, append_images=[second])

        blocks = preview(source, filename="report.pdf", pages="2-3")

        self.assertEqual([block["name"] for block in _images(blocks)], ["report-page-2.png"])
        self.assertTrue(any("pages unavailable: 3" in block.get("text", "") for block in blocks))
        self.assertEqual(_images(preview(source, filename="report.pdf", max_images=0)), [])

    def test_xlsx_preview_reads_sheet_choices_and_sample_cells(self):
        source = self.directory / "table.xlsx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("xl/workbook.xml", """
                <workbook xmlns:r="urn:relationships"><sheets>
                  <sheet name="Summary" r:id="rId1"/>
                </sheets></workbook>""")
            archive.writestr("xl/_rels/workbook.xml.rels", """
                <Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>""")
            archive.writestr("xl/worksheets/sheet1.xml", """
                <worksheet><dimension ref="A1:A2"/><sheetData>
                  <row r="1"><c r="A1" t="inlineStr"><is><t>Revenue</t></is></c></row>
                  <row r="2"><c r="A2"><v>25</v></c></row>
                </sheetData></worksheet>""")

        text = preview(source, sheet="Summary", automatic=True)[0]["text"]

        self.assertIn("Summary · A1:A2", text)
        self.assertIn('"A1": "Revenue"', text)
        self.assertIn('"A2": "25"', text)
