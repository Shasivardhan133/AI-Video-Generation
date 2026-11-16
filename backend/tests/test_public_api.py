"""Health, profiles, request IDs, and generic error responses."""

from django.contrib.sessions.models import Session

from .base import TempMediaTestCase


class HealthTests(TempMediaTestCase):
    def test_health_ok(self):
        response = self.client.get('/api/v1/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_health_does_not_create_a_session(self):
        self.client.get('/api/v1/health/')
        self.assertEqual(Session.objects.count(), 0)


class ProfileTests(TempMediaTestCase):
    def test_lists_both_profiles(self):
        response = self.client.get('/api/v1/profiles/')
        self.assertEqual(response.status_code, 200)
        profiles = {p['name']: p for p in response.json()['results']}
        self.assertEqual(set(profiles), {'wan2.1-t2v-1.3b-480p', 'wan2.1-i2v-14b-480p'})

        text = profiles['wan2.1-t2v-1.3b-480p']
        self.assertEqual(text['mode'], 'text_to_video')
        self.assertEqual(text['sizes'], [{'width': 832, 'height': 480}, {'width': 480, 'height': 832}])
        self.assertEqual(text['frame_counts'], list(range(17, 82, 4)))
        self.assertEqual(text['default_frame_count'], 81)
        self.assertEqual(text['fps'], 16)
        self.assertTrue(text['supports_prompt_enhancement'])

        image = profiles['wan2.1-i2v-14b-480p']
        self.assertEqual(image['mode'], 'image_to_video')
        self.assertFalse(image['supports_prompt_enhancement'])

    def test_each_mode_has_exactly_one_default_profile(self):
        # The frontend uses the default profile of each mode, as the backend does
        # when a request names no profile.
        results = self.client.get('/api/v1/profiles/').json()['results']
        defaults = {p['mode']: p['name'] for p in results if p['is_default'] is True}
        self.assertEqual(defaults, {'text_to_video': 'wan2.1-t2v-1.3b-480p', 'image_to_video': 'wan2.1-i2v-14b-480p'})
        self.assertEqual(sum(p['is_default'] is True for p in results), 2)
        self.assertTrue(all(isinstance(p['is_default'], bool) for p in results))

    def test_profiles_are_not_marked_t4_verified(self):
        for profile in self.client.get('/api/v1/profiles/').json()['results']:
            self.assertIs(profile['t4_verified'], False)


class RequestIdTests(TempMediaTestCase):
    def test_generated_when_missing(self):
        response = self.client.get('/api/v1/health/')
        self.assertRegex(response['X-Request-ID'], r'^[0-9a-f]{32}$')

    def test_safe_incoming_id_is_echoed(self):
        response = self.client.get('/api/v1/health/', headers={'X-Request-ID': 'trace-123.abc_X'})
        self.assertEqual(response['X-Request-ID'], 'trace-123.abc_X')

    def test_unsafe_incoming_id_is_replaced(self):
        for bad in ['has space', 'x' * 65, '<script>', 'a/b']:
            response = self.client.get('/api/v1/health/', headers={'X-Request-ID': bad})
            self.assertRegex(response['X-Request-ID'], r'^[0-9a-f]{32}$', bad)


class GenericErrorTests(TempMediaTestCase):
    def test_method_not_allowed(self):
        response = self.client.get('/api/v1/generations/text-to-video/')
        self.assertApiError(response, 405, 'method_not_allowed')

    def test_unsupported_media_type(self):
        response = self.client.post('/api/v1/generations/text-to-video/', 'prompt=x', content_type='text/plain')
        self.assertApiError(response, 415, 'unsupported_media_type')

    def test_malformed_json_body(self):
        response = self.client.post('/api/v1/generations/text-to-video/', '{"prompt": ', content_type='application/json')
        self.assertApiError(response, 400, 'invalid_request')
