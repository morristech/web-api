#! /usr/bin/env python3

import functools
import json
import logging
import random
import re
import string
import tornado.httpclient
import tornado.ioloop
import tornado.locks
import tornado.options
import tornado.web

from datetime import datetime, timedelta
from lxml import html
from tornado import gen


def random_string(length=20):
    alphabet = string.ascii_letters + string.digits + "_/-.;:#+*?()$[]!"
    return "".join((random.choice(alphabet) for i in range(length)))


class DataJsonHandler(tornado.web.RequestHandler):
    # 1 hour as a timeout is neither too outdated nor requires bothering
    # GitHub too often
    _timeout = timedelta(hours=1)

    # initialize with datetime that is outdated for sure
    _last_request = (datetime.now() - 2 * _timeout)

    # cache for last returned data
    _cached_response = None

    # request GitHub only once when multiple requests are made in parallel
    _lock = tornado.locks.Lock()

    # make sure to not send too many requests to the GitHub API to not trigger
    # the rate limit
    _last_failed_request = (datetime.now() - 2 * _timeout)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger("tornado.general")

    def add_default_headers(self):
        self.add_header("Content-Type", "text/plain")
        self.add_header("Access-Control-Allow-Origin", "newpipe.schabi.org")

    @classmethod
    def is_request_outdated(cls):
        now = datetime.now()

        if cls._cached_response is None:
            return True

        if (now - cls._last_request) > cls._timeout:
            return True

        return False

    @gen.coroutine
    def get(self):
        # ensure that timeout is respected
        now = datetime.now()

        if self.__class__._last_failed_request is not None and \
                (now - self.__class__._last_failed_request) < self.__class__._timeout:
            self.logger.log(logging.INFO,
                            "Request failed recently, waiting for timeout")
            self.add_default_headers()
            self.write_error(500)

        elif self.is_request_outdated():
            yield self.fetch_data_and_assemble_response()

        else:
            self.add_default_headers()
            self.write(self._cached_response)

    def validate_response(self, response: tornado.httpclient.HTTPResponse):
        if response.error:
            # release lock in case of errors
            self.__class__._lock.release()
            self.logger.log(
                logging.ERROR,
                "GitHub API error: {} -> {} ({})".format(response.effective_url, response.error, response.body),
            )
            self.send_error(500)
            return False

        return True

    @gen.coroutine
    def fetch_data_and_assemble_response(self):
        yield self.__class__._lock.acquire()

        self.logger.log(logging.INFO, "Fetching latest release from GitHub")

        releases_url_template = "https://gitlab.com/fdroid/fdroiddata/raw/master/metadata/{}.txt"
        stable_url = releases_url_template.format("org.schabi.newpipe")

        repo_url = "https://api.github.com/repos/TeamNewPipe/NewPipe"

        release_github_url = "https://github.com/TeamNewPipe/NewPipe/releases/"

        contributors_url = "https://github.com/TeamNewPipe/NewPipe"

        translations_url = "https://hosted.weblate.org/api/components/" \
                           "newpipe/strings/translations/"

        def make_request(url: str):
            kwargs = dict(headers={
                "User-Agent": ""
            })
            return tornado.httpclient.HTTPRequest(url, **kwargs)

        def fetch(request: tornado.httpclient.HTTPRequest):
            http_client = tornado.httpclient.AsyncHTTPClient()
            return http_client.fetch(request, raise_error=False)

        responses = yield tornado.gen.multi((
            fetch(make_request(repo_url)),
            fetch(make_request(stable_url)),
            fetch(make_request(release_github_url)),
            fetch(make_request(contributors_url)),
            fetch(make_request(translations_url)),
        ))

        for response in responses:
            if not self.validate_response(response):
                self.__class__._last_failed_request = datetime.now()
                return False

        repo_data, stable_data, release_github_data, \
        contributors_data, translations_data = [x.body for x in responses]

        def assemble_release_data(version_data: str):
            if isinstance(version_data, bytes):
                version_data = version_data.decode()

            versions = re.findall("commit=(.*)", version_data)
            version_codes = re.findall("Build:(.*)", version_data)
            version_code = version_codes[-1].split(",")[-1]
            return {
                "version": versions[-1],
                "version_code": int(version_code),
                "apk": "https://f-droid.org/repo/org.schabi.newpipe_" + version_code + ".apk",
            }

        repo = json.loads(repo_data)

        # scrape latest GitHub apk, version and version code from website
        # apk
        elem = html.fromstring(release_github_data)
        tags = elem.cssselect('.release-main-section li.d-block a[href$=".apk"]')
        if len(tags) == 0:
            release_github_apk = -1
        else:
            try:
                release_github_apk = "https://github.com" + tags[0].get('href')
            except:
                release_github_apk = -1

        # version
        tags = elem.cssselect(
            ".release .float-left ul li a.css-truncate > span.css-truncate-target")
        if len(tags) == 0:
            release_github_version = -1
        else:
            try:
                release_github_version = tags[0].text
            except:
                release_github_version = -1

        # version code
        # get git hash from release page
        tags = elem.cssselect(".release .float-left ul li a code")
        if len(tags) == 0:
            release_github_version_code = -1
        else:
            try:
                release_github_hash = tags[0].text

                # use git hash to get the matching build.gradle file
                response = yield tornado.gen.multi((
                    fetch(make_request("https://raw.githubusercontent.com/TeamNewPipe/NewPipe/" +
                                       release_github_hash + "/app/build.gradle")),
                ))
                gradle_file_data = [x.body for x in response]
                gradle_file_data = gradle_file_data[0]
                if isinstance(gradle_file_data, bytes):
                    gradle_file_data = gradle_file_data.decode()
                version_codes_g = re.findall("versionCode(.*)", gradle_file_data)
                release_github_version_code = version_codes_g[0].split(" ")[-1]
            except:
                release_github_version_code = -1

        # scrape contributors from website
        elem = html.fromstring(contributors_data)
        tags = elem.cssselect(".numbers-summary a[href$=contributors] .num")
        if len(tags) != 1:
            contributors_count = -1
        else:
            try:
                contributors_count = int(tags[0].text)
            except:
                contributors_count = -1

        translations = json.loads(translations_data)

        data = {
            "stats": {
                "stargazers": repo["stargazers_count"],
                "watchers": repo["subscribers_count"],
                "forks": repo["forks_count"],
                "contributors": contributors_count,
                "translations": int(translations["count"]),
            },
            "flavors": {
                "github": {
                    "stable": {
                        "version": release_github_version,
                        "version_code": int(release_github_version_code),
                        "apk": release_github_apk,
                    }
                },
                "fdroid": {
                    "stable": assemble_release_data(stable_data),
                }
            }
        }

        # update cache
        self.update_cache(data)

        # once cache is updated, release lock
        self.__class__._lock.release()

        # finish response
        self.add_default_headers()
        self.write(data)
        self.finish()

    @classmethod
    def update_cache(cls, data):
        cls._cached_response = data
        now = datetime.now()
        cls._last_request = now


def make_app():
    return tornado.web.Application([
        (r"/data.json", DataJsonHandler),
    ])


if __name__ == "__main__":
    tornado.options.parse_command_line()

    app = make_app()
    app.listen(3000)

    tornado.ioloop.IOLoop.current().start()
