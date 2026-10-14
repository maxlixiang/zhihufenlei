import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from zhihu_classifier.deepseek import DeepSeekClient, DeepSeekError
from zhihu_classifier.model_progress import ArticleProgress
from zhihu_classifier.cli import _print_item_progress, _print_copy_progress


class ProgressTests(unittest.TestCase):
    def client(self, retries=3):
        return DeepSeekClient(api_key="test-only",base_url="https://invalid.local",model="test",max_retries=retries)

    def response(self):
        value={"choices":[{"finish_reason":"stop","message":{"content":'{"ok":true}'}}],"usage":{}}
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps(value).encode()
        return response

    def test_timeout_retry_is_reported_and_saved_without_changing_result(self):
        events=[]
        with patch("zhihu_classifier.deepseek.urlopen",side_effect=[TimeoutError("private detail"),self.response()]) as request, \
             patch("zhihu_classifier.deepseek.time.sleep") as sleep:
            payload,envelope=self.client().complete_json(system_prompt="s",user_prompt="u",progress=events.append)
        self.assertEqual(payload,{"ok":True})
        self.assertEqual(request.call_count,2)
        sleep.assert_called_once_with(1)
        retry=next(e for e in events if e["state"]=="retry")
        self.assertIn("超时",retry["reason"])
        self.assertEqual(retry["next_attempt"],2)
        self.assertNotIn("private detail",str(events))
        self.assertEqual(envelope["request_diagnostics"]["attempts"],2)
        self.assertEqual(len(envelope["request_diagnostics"]["failures"]),1)

    def test_unauthorized_is_not_retried(self):
        error=HTTPError("https://invalid.local",401,"denied",{},io.BytesIO(b"private server detail"))
        events=[]
        with patch("zhihu_classifier.deepseek.urlopen",side_effect=error) as request:
            with self.assertRaises(DeepSeekError):
                self.client().complete_json(system_prompt="s",user_prompt="u",progress=events.append)
        self.assertEqual(request.call_count,1)
        self.assertEqual(events[-1]["state"],"failed")
        self.assertNotIn("private server detail",str(events))

    def test_display_exception_does_not_repeat_request(self):
        with patch("zhihu_classifier.deepseek.urlopen",return_value=self.response()) as request:
            payload,_=self.client().complete_json(system_prompt="s",user_prompt="u",progress=Mock(side_effect=OSError))
        self.assertEqual(payload,{"ok":True});self.assertEqual(request.call_count,1)

    def test_silent_request_reports_current_article_and_stops_thread(self):
        emitted=threading.Event();events=[]
        def callback(event):
            events.append(event)
            if event["state"]=="waiting":emitted.set()
        activity=ArticleProgress(callback,{"title":"当前文章","current":25,"total":27},interval=.01)
        with activity:
            activity.notify({"state":"request","attempt":2,"max_attempts":3})
            self.assertTrue(emitted.wait(2))
        self.assertFalse(activity.thread.is_alive())
        event=next(e for e in events if e["state"]=="waiting")
        self.assertEqual(event["title"],"当前文章");self.assertEqual(event["attempt"],2)
        self.assertIn("elapsed_seconds",event)

    def test_thread_stops_when_request_fails(self):
        activity=ArticleProgress(lambda e:None,{"title":"文章"},interval=.01)
        with self.assertRaises(ValueError):
            with activity:raise ValueError("failure")
        self.assertFalse(activity.thread.is_alive())

    def test_progress_lines_have_immediate_newline_and_retry_context(self):
        event={"phase":"full_analysis","current":25,"total":27,"processed":24,"errors":0,"title":"测试文章"}
        with redirect_stdout(io.StringIO()) as output:
            _print_item_progress({**event,"state":"retry","elapsed_seconds":120,
                "attempt":1,"next_attempt":2,"max_attempts":3,"retry_delay":1,"reason":"网络请求超时"})
            _print_item_progress({**event,"processed":25,"elapsed_seconds":131})
            _print_copy_progress({"current":1,"total":27,"copied":1,"existing":0,"errors":0,"title":"文章"})
        text=output.getvalue()
        self.assertEqual(len(text.splitlines()),3)
        self.assertNotIn("\r",text)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("当前 25/27",text);self.assertIn("120.0 秒",text);self.assertIn("重试",text)
