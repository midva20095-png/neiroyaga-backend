import unittest

from kie_site import build_chat, build_image_task, compact_prompt, image_mime, task_view, text_from_response, upload_view

FLUX = {
    "kieModel": "flux-3/klein-text-to-image",
    "kieEdit": "flux-3/klein-edit-image",
    "kieImage": "image_urls",
    "kieAspect": "aspect_ratio",
    "kieSize": "resolution",
    "aspects": ["1:1", "16:9", "9:16"],
    "qualities": [{"id": "1K", "credits": 3}, {"id": "4K", "credits": 10}],
    "quality": "1K",
    "kieExtra": {"output_format": "jpeg", "num_images": 1},
}

IDEO = {
    "kieModel": "ideogram/v3-text-to-image",
    "kieAspect": "image_size",
    "aspects": ["1:1", "16:9"],
}


class KieSiteTests(unittest.TestCase):
    def test_text_image_uses_cheap_size(self):
        task = build_image_task(FLUX, "кот", "16:9", None, [])
        self.assertEqual(task["model"], "flux-3/klein-text-to-image")
        self.assertEqual(task["input"]["aspect_ratio"], "16:9")
        self.assertEqual(task["input"]["resolution"], "1K")
        self.assertEqual(task["input"]["output_format"], "jpeg")
        self.assertNotIn("image_urls", task["input"])

    def test_photo_switches_to_edit_model(self):
        task = build_image_task(FLUX, "сделай ночь", "1:1", "4K", ["https://cdn.example/a.jpg"])
        self.assertEqual(task["model"], "flux-3/klein-edit-image")
        self.assertEqual(task["input"]["resolution"], "4K")
        self.assertEqual(task["input"]["image_urls"], ["https://cdn.example/a.jpg"])

    def test_text_only_model_rejects_photo(self):
        with self.assertRaises(ValueError):
            build_image_task(IDEO, "вывеска", "1:1", None, ["https://cdn.example/a.jpg"])

    def test_ideogram_size_name(self):
        task = build_image_task(IDEO, "вывеска", "16:9", None, [])
        self.assertEqual(task["input"]["image_size"], "landscape_16_9")
        self.assertEqual(task["input"]["rendering_speed"], "TURBO")

    def test_unknown_aspect_falls_back(self):
        task = build_image_task(FLUX, "кот", "2:3", "nope", [])
        self.assertEqual(task["input"]["aspect_ratio"], "1:1")
        self.assertEqual(task["input"]["resolution"], "1K")

    def test_chat_bodies(self):
        deep = build_chat({"kieChat": {"style": "responses", "path": "/openai/v1/responses", "model": "deepseek-v4-1-flash", "maxTokens": 800, "maxChars": 20}}, "а" * 40)
        self.assertEqual(deep["path"], "/openai/v1/responses")
        self.assertEqual(len(deep["body"]["input"]), 20)
        self.assertIn("retry", deep)
        haiku = build_chat({"kieChat": {"style": "claude", "path": "/claude/v1/messages", "model": "claude-haiku-5-5", "maxTokens": 700}}, "привет")
        self.assertEqual(haiku["body"]["messages"][0]["content"], "привет")

    def test_text_and_task_views(self):
        text = text_from_response("responses", {"output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "готово"}]}]})
        self.assertEqual(text, "готово")
        view = task_view({"data": {"state": "success", "resultJson": '{"resultUrls":["https://cdn.example/out.jpg"]}'}})
        self.assertEqual(view["urls"], ["https://cdn.example/out.jpg"])
        self.assertEqual(upload_view({"code": 200, "data": {"downloadUrl": "https://cdn.example/in.jpg"}})["url"], "https://cdn.example/in.jpg")
        self.assertEqual(upload_view({"code": 500, "data": {"downloadUrl": "https://cdn.example/in.jpg"}})["url"], "")

    def test_prompt_keeps_the_latest_question(self):
        history = [{"role": "user", "text": "старое"}, {"role": "model", "text": "ответ"}, {"role": "user", "text": "новое"}]
        packed = compact_prompt("новое", history, limit=12)
        self.assertTrue(packed.endswith("новое"))

    def test_image_mime(self):
        self.assertEqual(image_mime(b"\xff\xd8\xff\x00"), "image/jpeg")
        self.assertEqual(image_mime(b"not-an-image"), "")


if __name__ == "__main__":
    unittest.main()
