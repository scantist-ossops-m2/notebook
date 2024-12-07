import json

from tornado import web

from ...base.handlers import APIHandler

class NbconvertRootHandler(APIHandler):

    @web.authenticated
    def get(self):
        self.check_xsrf_cookie()
        try:
            from nbconvert.exporters.export import exporter_map
        except ImportError as e:
            raise web.HTTPError(500, "Could not import nbconvert: %s" % e)
        res = {}
        for format, exporter in exporter_map.items():
            res[format] = info = {}
            info['output_mimetype'] = exporter.output_mimetype

        self.finish(json.dumps(res))

default_handlers = [
    (r"/api/nbconvert", NbconvertRootHandler),
]
