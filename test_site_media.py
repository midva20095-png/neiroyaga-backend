import unittest

import base64

from site_media import SITE_SIZES, audio_seconds, cover_bytes, media_file, shown_cover, site_sizes


def wav(seconds=1, rate=8000):
    samples = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + (36 + len(samples)).to_bytes(4, "little") + b"WAVE"
    fmt = b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
    fmt += rate.to_bytes(4, "little") + (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    return header + fmt + b"data" + len(samples).to_bytes(4, "little") + samples


class MediaFileTest(unittest.TestCase):
    def test_images(self):
        self.assertEqual(media_file(b"\xff\xd8\xff\x00", "application/octet-stream", "image", 2), ("photo_2.jpg", "image/jpeg"))
        self.assertEqual(media_file(b"\x89PNG\r\n\x1a\nrest", "", "image"), ("photo_1.png", "image/png"))
        webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"xxxx"
        self.assertEqual(media_file(webp, "image/jpeg", "image", 3)[0], "photo_3.webp")

    def test_audio(self):
        self.assertEqual(media_file(wav(), "application/octet-stream", "audio"), ("voice.wav", "audio/wav"))
        self.assertEqual(media_file(b"ID3rest", "", "audio")[0], "voice.mp3")
        self.assertEqual(media_file(b"OggSrest", "", "audio")[0], "voice.ogg")
        self.assertEqual(media_file(b"fLaCrest", "", "audio")[0], "voice.flac")
        self.assertIsNone(media_file(b"\x1aE\xdf\xa3webm", "audio/webm", "audio")[0])

    def test_cover(self):
        self.assertEqual(cover_bytes(""), ("", None, None))
        self.assertEqual(cover_bytes("https://cdn.example/a.jpg"), ("https://cdn.example/a.jpg", None, None))
        raw = base64.b64encode(b"\xff\xd8\xff\x00").decode()
        url, data, mime = cover_bytes("data:image/jpeg;base64," + raw)
        self.assertIsNone(url)
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertEqual(mime, "image/jpeg")
        with self.assertRaises(ValueError):
            cover_bytes("data:image/jpeg;base64," + base64.b64encode(b"not-a-photo").decode())

    def test_sizes(self):
        self.assertEqual(site_sizes(""), list(SITE_SIZES))
        self.assertEqual(site_sizes("3:4"), ["3:4"])
        self.assertEqual(site_sizes("9:16, 1:1"), ["9:16", "1:1"])
        self.assertEqual(site_sizes("что угодно"), list(SITE_SIZES))
        self.assertEqual(site_sizes("", True), [])

    def test_cover_link_changes_after_replace(self):
        old = "https://neiroyaga-backend-production.up.railway.app/api/media/templates/portret.jpg"
        self.assertTrue(shown_cover(old).endswith("?v=1"))
        fresh = old + "?v=171000"
        self.assertEqual(shown_cover(fresh), fresh)
        self.assertEqual(shown_cover("https://cdn.example/a.jpg"), "https://cdn.example/a.jpg")

    def test_wav_length(self):
        self.assertAlmostEqual(audio_seconds(wav(2, 8000)), 2.0)


if __name__ == "__main__":
    unittest.main()
