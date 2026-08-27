"""
Tests for the Phase 1 HTTP surface.

These assert the API *contract* - status codes and the exact camelCase keys
Angular reads - because the frontend's TypeScript interfaces are written against
them. A rename here that is not caught is a runtime break there.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from reviews.evaluation.marking_scheme import get_active_marking_scheme


class HealthEndpointTests(TestCase):
    def test_health_reports_ok_when_dependencies_are_reachable(self) -> None:
        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "ai-code-review-assistant")
        self.assertEqual(payload["markingSchemeVersion"], get_active_marking_scheme().version)
        self.assertEqual(payload["checks"]["database"]["status"], "ok")
        self.assertEqual(payload["checks"]["cache"]["status"], "ok")

    def test_health_is_reachable_by_route_name(self) -> None:
        self.assertEqual(reverse("health"), "/api/health/")

    def test_health_rejects_post(self) -> None:
        response = self.client.post("/api/health/")
        self.assertEqual(response.status_code, 405)


class EvaluationCriteriaEndpointTests(TestCase):
    def setUp(self) -> None:
        self.scheme = get_active_marking_scheme()
        self.response = self.client.get("/api/evaluation-criteria/")
        self.payload = self.response.json()

    def test_returns_ok(self) -> None:
        self.assertEqual(self.response.status_code, 200)

    def test_exposes_version_and_maximum(self) -> None:
        self.assertEqual(self.payload["version"], self.scheme.version)
        self.assertEqual(self.payload["maxScore"], 100)

    def test_exposes_every_category_in_scheme_order(self) -> None:
        names = [category["name"] for category in self.payload["categories"]]
        self.assertEqual(names, list(self.scheme.category_names))

    def test_category_shape_matches_the_typescript_contract(self) -> None:
        first = self.payload["categories"][0]
        self.assertEqual(set(first), {"key", "name", "maxScore", "description"})
        self.assertEqual(first["maxScore"], 25)

    def test_category_maximums_sum_to_the_published_maximum(self) -> None:
        total = sum(category["maxScore"] for category in self.payload["categories"])
        self.assertEqual(total, self.payload["maxScore"])

    def test_grade_band_shape_matches_the_typescript_contract(self) -> None:
        bands = self.payload["gradeBands"]
        self.assertEqual(len(bands), 5)
        self.assertEqual(
            set(bands[0]),
            {"grade", "band", "minScore", "maxScore", "meaning"},
        )
        self.assertEqual(bands[0]["grade"], "A")
        self.assertEqual(bands[0]["minScore"], 90)

    def test_endpoint_is_read_only(self) -> None:
        response = self.client.post("/api/evaluation-criteria/", data={}, content_type="application/json")
        self.assertEqual(response.status_code, 405)


class CorsTests(TestCase):
    def test_allowed_origin_receives_cors_header(self) -> None:
        """Without this header the browser blocks every Angular API call."""
        response = self.client.get("/api/health/", HTTP_ORIGIN="http://localhost:4200")
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "http://localhost:4200",
        )

    def test_disallowed_origin_receives_no_cors_header(self) -> None:
        response = self.client.get("/api/health/", HTTP_ORIGIN="http://evil.example.com")
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
