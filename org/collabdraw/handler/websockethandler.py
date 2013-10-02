import logging
import json
import threading
import subprocess
import uuid
from zlib import compress
from urllib.parse import quote
import config

import os
from base64 import b64encode
import tornado.websocket
import tornado.web
import redis
from pystacia import read
from ..tools.tools import hexColorToRGB, createCairoContext


class RealtimeHandler(tornado.websocket.WebSocketHandler):
    room_name = ''
    paths = []
    redis_client = None
    page_no = 1
    num_pages = 1

    def construct_key(self, namespace, key, *keys):
        publish_key = ""
        if len(keys) == 0:
            publish_key = "%s:%s" % (namespace, key)
        else:
            publish_key = "%s:%s:%s" % (namespace, key, ":".join(keys))
        return publish_key

    def redis_listener(self, room_name, page_no):
        self.logger.info("Starting listener thread for room %s" % room_name)
        rr = redis.Redis(host=config.REDIS_IP_ADDRESS, port=config.REDIS_PORT, db=1)
        r = rr.pubsub()
        r.subscribe(self.construct_key(room_name, page_no))
        for message in r.listen():
            for listener in self.application.LISTENERS.get(room_name, {}).get(page_no, []):
                self.logger.debug("Sending message to room %s" % room_name)
                listener.send_message(message['data'])

    def open(self):
        self.logger = logging.getLogger('websocket')
        self.logger.info("Open connection")
        self.send_message(self.construct_message("ready"))
        self.redis_client = redis.Redis(host=config.REDIS_IP_ADDRESS, db=2)

    def on_message(self, message):
        m = json.loads(message)
        event = m.get('event', '').strip()
        data = m.get('data', {})

        self.logger.debug("Processing event %s" % event)
        if not event:
            self.logger.error("No event specified")
            return

        if event == "init":
            self.logger.info("Initializing with room name %s" % self.room_name)
            room_name = data.get('room', '')
            if not room_name:
                self.logger.error("Room name not provided. Can't initialize")
                return
            page_no = data.get('page', '1')

            self.init(room_name, page_no)

        elif event == "draw-click":
            singlePath = data['singlePath']
            if not self.paths:
                self.logger.debug("None")
                self.paths = []

            self.paths.extend(singlePath)
            self.broadcast_message(self.construct_message("draw", {'singlePath': singlePath}))
            self.redis_client.set(self.construct_key(self.room_name, self.page_no), self.paths)

        elif event == "clear":
            self.broadcast_message(self.construct_message("clear"))
            self.redis_client.delete(self.construct_key(self.room_name, self.page_no))

        elif event == "get-image":
            if self.room_name != data['room'] or self.page_no != data['page']:
                self.logger.warning("Room name %s and/or page no. %s doesn't match with current room name %s and/or",
                                    "page no. %s. Ignoring" % (
                                    data['room'], data['page'], self.room_name, self.page_no))
            image_url, width, height = self.get_image_data(self.room_name, self.page_no)
            self.send_message(self.construct_message("image", {'url': image_url,
                                                               'width': width, 'height': height}))

        elif event == "video":
            self.make_video(self.room_name, self.page_no)

        elif event == "new-page":
            self.logger.info("num_pages was %d" % self.num_pages)
            self.redis_client.set(self.construct_key("info", self.room_name, "npages"),
                                  self.num_pages + 1)
            self.num_pages += 1
            self.logger.info("num_pages is now %d" % self.num_pages)
            self.init(self.room_name, self.num_pages)

    def on_close(self):
        self.leave_room(self.room_name)

    def construct_message(self, event, data={}):
        m = json.dumps({"event": event, "data": data})
        return m

    def broadcast_message(self, message):
        self.leave_room(self.room_name, False)
        self.redis_client.publish(self.construct_key(self.room_name, self.page_no), message)
        self.join_room(self.room_name)

    def send_message(self, message):
        if type(message) == type(b''):
            self.logger.info("Decoding binary string")
            message = message.decode('utf-8')
        elif type(message) != type(''):
            self.logger.info("Converting message from %s to %s" % (type(message),
                                                                   type('')))
            message = str(message)
        message = b64encode(compress(bytes(quote(message), 'utf-8'), 9))
        self.write_message(message)

    def leave_room(self, room_name, clear_paths=True):
        self.logger.info("Leaving room %s" % room_name)
        if self in self.application.LISTENERS.get(room_name, {}).get(self.page_no, []):
            self.application.LISTENERS[room_name][self.page_no].remove(self)
        if clear_paths:
            self.paths = []

    def join_room(self, room_name):
        self.logger.info("Joining room %s" % room_name)
        self.application.LISTENERS.setdefault(room_name, {}).setdefault(self.page_no, []).append(self)

    def init(self, room_name, page_no):
        self.logger.info("Initializing %s and %s" % (room_name, page_no))
        if room_name not in self.application.LISTENERS or page_no not in self.application.LISTENERS[room_name]:
            t = threading.Thread(target=self.redis_listener, args=(room_name, page_no))
            t.start()
            self.application.LISTENER_THREADS.setdefault(room_name, {}).setdefault(page_no, []).append(t)

        self.leave_room(self.room_name)
        self.room_name = room_name
        self.page_no = page_no
        self.join_room(self.room_name)

        n_pages = self.redis_client.get(self.construct_key("info", self.room_name, "npages"))
        if n_pages:
            self.num_pages = int(n_pages.decode('utf-8'))
            # First send the image if it exists
        image_url, width, height = self.get_image_data(self.room_name, self.page_no)
        self.send_message(self.construct_message("image", {'url': image_url,
                                                           'width': width, 'height': height}))
        # Then send the paths
        p = self.redis_client.get(self.construct_key(self.room_name, self.page_no))
        if p:
            self.paths = json.loads(p.decode('utf-8').replace("'", '"'))
        else:
            self.paths = []
            self.logger.info("No data in database")
        self.send_message(self.construct_message("draw-many",
                                                 {'datas': self.paths, 'npages': self.num_pages}))

    def get_image_data(self, room_name, page_no):
        image_url = os.path.join("files", room_name, str(page_no) + "_image.png")
        image_path = os.path.join(config.ROOT_DIR, image_url)
        try:
            image = read(image_path)
        except IOError as e:
            self.logger.error("Error %s while reading image at location %s" % (e,
                                                                               image_path))
            return '', -1, -1
        width, height = image.size
        return image_url, width, height

    def make_video(self, room_name, page_no):
        p = self.redis_client.get(self.construct_key(room_name, page_no))
        tmp_path = os.path.join(config.ROOT_DIR, "tmp")
        os.makedirs(tmp_path, exist_ok=True)
        path_prefix = os.path.join(tmp_path, str(uuid.uuid4()))
        if p:
            points = json.loads(p.decode('utf-8').replace("'", '"'))
            i = 0
            c = createCairoContext(920, 550)
            for point in points:
                c.set_line_width(float(point['lineWidth'].replace('px', '')))
                c.set_source_rgb(*hexColorToRGB(point['lineColor']))
                if point['type'] == 'dragstart' or point['type'] == 'touchstart':
                    c.move_to(point['oldx'], point['oldy'])
                elif point['type'] == 'drag' or point['type'] == 'touchmove':
                    c.move_to(point['oldx'], point['oldy'])
                    c.line_to(point['x'], point['y'])
                c.stroke()
                f = open(path_prefix + "_img_" + str(i) + ".png", "wb")
                c.get_target().write_to_png(f)
                f.close()
                i += 1
            video_file_name = path_prefix + '_video.mp4'
            retval = subprocess.call(['ffmpeg', '-f', 'image2', '-i', path_prefix + '_img_%d.png', video_file_name])
            self.logger.info("Image for room %s and page %s successfully created. File name is %s" % (
            room_name, page_no, video_file_name))
            if retval == 0:
                # Clean up if successfull
                cleanup_files = path_prefix + '_img_*'
                self.logger.info("Cleaning up %s" % cleanup_files)
                subprocess.call(['rm', cleanup_files])