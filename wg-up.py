#!/usr/bin/env python3

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs
import subprocess
import re
import shutil
import os


# parameters


# make http server available in wireguard subnet via proxy
PROXY = True

# wireguard server (full configuration in the docker run command below)
WG_IP = "192.168.53.128"
PEERS_PATH = "./peers.txt"
WG_CONFIG_PATH = "./config"

# web server
DOMAIN_NAME = "wireguard.local"  # only requred when PROXY is true
"""
publishing options
0.0.0.0     accessible on all interfaces - clients in the same network or on the internet will have accesss
127.0.0.1   accessible locally via loopback interface - accessible only from your machine (compatible with PROXY)
"""
HTTP_IP = "127.0.0.1"


# html


index_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width">
    <title>Wireguard User Management</title>
    <link rel="stylesheet" type="text/css" href="stylesheet.css">
    <script>
        function show_qrcode(username, form) {{
            form.insertAdjacentHTML('afterend', `<img src="${{window.location}}config/peer_${{username}}/peer_${{username}}.png" width="300px">`)
        }}
    </script>
</head>
<body>
    <h1>Wireguard User Management</h1>
    <form action="/add_user" method="post" class="add-user">
        <input type="text" autocomplete="off" name="username"/>
        <input type="submit" value="add user"/>
    </form>
    <div class="user-list">
        USER_ITEMS
    </div>
</body>
</html>
"""

user_item_html = """<div class="user-item">
            <form action="/delete_user" method="post" class="delete-user">
                <label>USERNAME</label>
                <input type="hidden" name="username" value="USERNAME"/>
                <input type="submit" value="delete"/>
                <button type="button" onclick='show_qrcode("USERNAME", this.form)'>QR code</button>
            </form>
        </div>
"""


# functions


def get_peers(path):
    with open(path, "r") as file:
        peers = file.read()
    peers_list = [user for user in peers.split("\n") if user.strip() != ""]
    return peers_list

def set_peers(path, peers_list):
    peers_list.sort(key=lambda x: (x.lower(), x))
    peers_string = "\n".join(peers_list)
    with open(path, "w") as file:
        file.write(peers_string)

def restart_service(peers):
    global WG_CONFIG_PATH
    global WG_IP
    global PROXY

    peers = ",".join(peers)
    print("service restarting")
    try:
        script = f"""
        docker stop simple-wireguard || true
        docker run -d \\
        --rm \\
        --name=simple-wireguard \\
        --cap-add=NET_ADMIN \\
        --cap-add=SYS_MODULE \\
        -e PUID=1000 \\
        -e PGID=1000 \\
        -e TZ=Etc/UTC \\
        -e SERVERURL={WG_IP} \\
        -e SERVERPORT=51820 \\
        -e PEERS={peers} \\
        -e PEERDNS=auto \\
        -e INTERNAL_SUBNET=10.13.13.0 \\
        -e ALLOWEDIPS=0.0.0.0/0 \\
        -e PERSISTENTKEEPALIVE_PEERS= \\
        -e LOG_CONFS=true \\
        -p 51820:51820/udp \\
        -v {WG_CONFIG_PATH}:/config \\
        -v /lib/modules:/lib/modules \\
        --sysctl="net.ipv4.conf.all.src_valid_mark=1" \\
        simple-wireguard"""
        if PROXY:
            script += f""" \\
            sh -c 'socat TCP4-LISTEN:80,fork,crlf,reuseaddr,bind=$(ip addr show wg0 | awk "/inet / {{print \\$2}}" | cut -d/ -f1) TCP4:$(ip -4 route show default | awk "{{print \\$3}}"):80'
            """
        # SERVERURL is the public ip address that clients connect to to gain access to the VPN subnet
        # INTERNAL_SUBNET defines the internal subnet of the vpn. the server has an ip address ending with .1
        subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)
    except:
        print("restarting service failed")


# http server


class RequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith(".css") or self.path.endswith(".png"):
            return super().do_GET()
        global index_html
        global user_item_html
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        peers_list = get_peers(PEERS_PATH)
        peers_list_html = ""
        for username in peers_list:
            peers_list_html += "\n" + user_item_html.replace("USERNAME", username)
        final_html = index_html.replace("USER_ITEMS", peers_list_html)
        self.wfile.write(final_html.encode())
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        params = parse_qs(post_data)
        if "username" in params:
            username = params['username'][0]
            if self.path == "/add_user":
                username = username.replace("ö", "oe")
                username = username.replace("ä", "ae")
                username = username.replace("ü", "ue")
                filtered_user = re.sub(r'[^a-zA-Z0-9]', '', username)[:32].strip()
                print(f"adding user {filtered_user}")
                peers_list = get_peers(PEERS_PATH)
                if filtered_user not in peers_list:
                    peers_list.append(filtered_user)
                    set_peers(PEERS_PATH, peers_list)
                    print(f"new peers list written: {peers_list}")
                restart_service(peers_list)
            elif self.path == "/delete_user":
                print(f"deleting user {username}")
                peers_list = get_peers(PEERS_PATH)
                peers_list.remove(username)
                set_peers(PEERS_PATH, peers_list)
                print(f"new peers list written: {peers_list}")
                restart_service(peers_list)
                try:
                    user_config_path = os.path.join(WG_CONFIG_PATH, f"peer_" + username)
                    shutil.rmtree(user_config_path, ignore_errors=True)
                    print(f"removed user directory {user_config_path}")
                except Exception as e:
                    print(f"An error occurred while removing user directory {user_config_path}: {e}")
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()

def run():
    global WG_CONFIG_PATH
    global HTTP_IP
    global PEERS_PATH
    global DOMAIN_NAME
    global PROXY

    if PROXY:
        # add dns entry of the http server
        os.makedirs(os.path.join(WG_CONFIG_PATH, "coredns"), exist_ok=True)
        with open(os.path.join(WG_CONFIG_PATH, "coredns/Corefile"), "w") as file:
            file.write(f""". {{
            loop
            errors
            health
            hosts {{
                10.13.13.1 {DOMAIN_NAME}
                fallthrough
            }}
            forward . /etc/resolv.conf
        }}""")

    port = 80
    server_address = (HTTP_IP, port)
    httpd = HTTPServer(server_address, RequestHandler)
    print("starting wireguard docker...")
    restart_service(get_peers(PEERS_PATH))
    print(f"starting server on port {port}...")
    httpd.serve_forever()

run()
