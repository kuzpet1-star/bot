from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

port = 3000
server = HTTPServer(("0.0.0.0", port), Handler)
print("Server started")
server.serve_forever()
