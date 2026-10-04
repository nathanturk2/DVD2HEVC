from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app.extract import (
    SectorBatchSink,
    SectorRangeSink,
    extract_domain_sector_ranges,
    extract_domain_sector_segment_sets,
)
from dvd2hevc_app.vob import DVD_SECTOR_SIZE


class SectorRangeSinkTests(unittest.TestCase):
    def test_range_crossing_input_chunks_is_exact(self) -> None:
        source = b"".join(bytes([number]) * DVD_SECTOR_SIZE for number in range(5))
        output = io.BytesIO()
        sink = SectorRangeSink(output, 1, 3)
        sink.write(source[:2500])
        sink.write(source[2500:7000])
        sink.write(source[7000:])
        self.assertEqual(output.getvalue(), source[DVD_SECTOR_SIZE : 4 * DVD_SECTOR_SIZE])
        self.assertEqual(sink.written, 3 * DVD_SECTOR_SIZE)

    def test_whole_preceding_file_can_be_skipped(self) -> None:
        output = io.BytesIO()
        sink = SectorRangeSink(output, 2, 2)
        sink.advance(DVD_SECTOR_SIZE)
        sink.advance(DVD_SECTOR_SIZE)
        sink.write(b"x" * DVD_SECTOR_SIZE)
        self.assertEqual(sink.position, 3 * DVD_SECTOR_SIZE)
        self.assertEqual(sink.written, DVD_SECTOR_SIZE)

    def test_batch_sink_fans_one_pass_into_independent_and_overlapping_cells(self) -> None:
        source = b"".join(bytes([number]) * DVD_SECTOR_SIZE for number in range(6))
        first = io.BytesIO()
        second = io.BytesIO()
        overlapping = io.BytesIO()
        sink = SectorBatchSink([
            (first, 0, 1),
            (second, 4, 5),
            (overlapping, 1, 4),
        ])
        sink.write(source[:3000])
        sink.write(source[3000:8500])
        sink.write(source[8500:])
        self.assertEqual(first.getvalue(), source[: 2 * DVD_SECTOR_SIZE])
        self.assertEqual(second.getvalue(), source[4 * DVD_SECTOR_SIZE :])
        self.assertEqual(
            overlapping.getvalue(),
            source[DVD_SECTOR_SIZE : 5 * DVD_SECTOR_SIZE],
        )
        self.assertEqual(sink.written, [2 * DVD_SECTOR_SIZE, 2 * DVD_SECTOR_SIZE, 4 * DVD_SECTOR_SIZE])
        self.assertEqual(sink.consumed, len(source))

    def test_batch_sink_can_skip_a_whole_non_intersecting_vob(self) -> None:
        output = io.BytesIO()
        sink = SectorBatchSink([(output, 3, 3)])
        self.assertFalse(sink.intersects(0, 2 * DVD_SECTOR_SIZE))
        sink.advance(2 * DVD_SECTOR_SIZE)
        self.assertTrue(sink.intersects(2 * DVD_SECTOR_SIZE, 4 * DVD_SECTOR_SIZE))
        sink.write(b"x" * 2 * DVD_SECTOR_SIZE)
        self.assertEqual(output.getvalue(), b"x" * DVD_SECTOR_SIZE)

    def test_batch_extractor_opens_iso_once_and_streams_each_vob_once(self) -> None:
        class Entry:
            def __init__(self, name: str, data: bytes) -> None:
                self.name = name
                self.data = data

            def get_data_length(self) -> int:
                return len(self.data)

        class Image:
            def __init__(self, entries: list[Entry]) -> None:
                self.entries = {entry.name: entry for entry in entries}
                self.reads: list[str] = []

            def get_file_from_iso_fp(self, sink: SectorBatchSink, *, udf_path: str) -> None:
                name = udf_path.rsplit("/", 1)[-1]
                self.reads.append(name)
                data = self.entries[name].data
                midpoint = len(data) // 2
                sink.write(data[:midpoint])
                sink.write(data[midpoint:])

        vob1 = Entry("VTS_01_1.VOB", b"".join(
            bytes([number]) * DVD_SECTOR_SIZE for number in range(3)
        ))
        vob2 = Entry("VTS_01_2.VOB", b"".join(
            bytes([number]) * DVD_SECTOR_SIZE for number in range(3, 6)
        ))
        image = Image([vob1, vob2])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.iso"
            source.write_bytes(b"iso")
            first = root / "first.vob"
            second = root / "second.vob"
            with (
                patch("dvd2hevc_app.extract.open_iso_image", return_value=image) as opened,
                patch("dvd2hevc_app.extract.close_iso_image") as closed,
                patch("dvd2hevc_app.extract.domain_vob_entries", return_value=[vob1, vob2]),
                patch("dvd2hevc_app.extract._entry_name", side_effect=lambda entry: entry.name),
                patch("dvd2hevc_app.extract.inspect_vob_file", return_value={"packs": 1}),
            ):
                reports = extract_domain_sector_ranges(
                    source,
                    domain="title",
                    vts=1,
                    ranges=[(1, 3, first), (4, 5, second)],
                )

            opened.assert_called_once_with(source.resolve())
            closed.assert_called_once_with(image)
            self.assertEqual(image.reads, ["VTS_01_1.VOB", "VTS_01_2.VOB"])
            combined = vob1.data + vob2.data
            self.assertEqual(
                first.read_bytes(), combined[DVD_SECTOR_SIZE : 4 * DVD_SECTOR_SIZE]
            )
            self.assertEqual(second.read_bytes(), combined[4 * DVD_SECTOR_SIZE :])
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(row["batch_extraction"]["iso_opens"] == 1 for row in reports))
            self.assertTrue(all(row["batch_extraction"]["ranges"] == 2 for row in reports))

    def test_segmented_batch_output_concatenates_extents_in_physical_order(self) -> None:
        class Entry:
            name = "VTS_01_1.VOB"

            def __init__(self, data: bytes) -> None:
                self.data = data

            def get_data_length(self) -> int:
                return len(self.data)

        class Image:
            def __init__(self, entry: Entry) -> None:
                self.entry = entry
                self.reads = 0

            def get_file_from_iso_fp(self, sink: SectorBatchSink, *, udf_path: str) -> None:
                self.reads += 1
                sink.write(self.entry.data)

        source_bytes = b"".join(
            bytes([number]) * DVD_SECTOR_SIZE for number in range(7)
        )
        entry = Entry(source_bytes)
        image = Image(entry)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.iso"
            source.write_bytes(b"iso")
            segmented = root / "segmented.vob"
            contiguous = root / "contiguous.vob"
            with (
                patch("dvd2hevc_app.extract.open_iso_image", return_value=image),
                patch("dvd2hevc_app.extract.close_iso_image"),
                patch("dvd2hevc_app.extract.domain_vob_entries", return_value=[entry]),
                patch("dvd2hevc_app.extract._entry_name", return_value=entry.name),
                patch("dvd2hevc_app.extract.inspect_vob_file", return_value={"packs": 1}),
            ):
                reports = extract_domain_sector_segment_sets(
                    source,
                    domain="title",
                    vts=1,
                    requests=[
                        ([(0, 1), (5, 6)], segmented),
                        ([(2, 4)], contiguous),
                    ],
                )
            self.assertEqual(image.reads, 1)
            self.assertEqual(
                segmented.read_bytes(),
                source_bytes[: 2 * DVD_SECTOR_SIZE]
                + source_bytes[5 * DVD_SECTOR_SIZE :],
            )
            self.assertEqual(
                contiguous.read_bytes(),
                source_bytes[2 * DVD_SECTOR_SIZE : 5 * DVD_SECTOR_SIZE],
            )
            self.assertEqual(reports[0]["segment_count"], 2)
            self.assertEqual(reports[0]["sector_count"], 4)
            self.assertEqual(reports[0]["segments"][1]["output_first_sector"], 2)
            self.assertEqual(reports[0]["batch_extraction"]["ranges"], 3)


if __name__ == "__main__":
    unittest.main()
