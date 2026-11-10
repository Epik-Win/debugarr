import unittest

from api_docs import load_openapi_document, swagger_ui_html


class ApiDocsTests(unittest.TestCase):
    def test_openapi_lists_api_routes_and_authentication(self):
        document = load_openapi_document("/debuggarr/seerr-webhook")

        self.assertEqual(document["openapi"], "3.0.3")
        self.assertIn("/api/bugs", document["paths"])
        self.assertIn("/api/notify", document["paths"])
        self.assertIn("/api/reviews/{review_id}/withdraw", document["paths"])
        self.assertIn("/api/mediacheck/reviews", document["paths"])
        self.assertIn("X-API-Key", {
            scheme["name"]
            for scheme in document["components"]["securitySchemes"].values()
        })

    def test_openapi_uses_configured_webhook_path(self):
        document = load_openapi_document("/hooks/seerr")

        self.assertIn("/hooks/seerr", document["paths"])
        self.assertNotIn("/debuggarr/seerr-webhook", document["paths"])

    def test_swagger_ui_loads_the_local_openapi_document(self):
        page = swagger_ui_html()

        self.assertIn("swagger-ui-dist", page)
        self.assertIn('url: "/api/openapi.json"', page)


if __name__ == "__main__":
    unittest.main()
