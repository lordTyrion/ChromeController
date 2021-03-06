""" Client for the Google Chrome browser's remote debugging api.

	> a = Shell(host='localhost', port=92222)


"""
import json
import socket
import time
import pprint
import logging
import os.path
import requests
import traceback
import signal
import websocket
import subprocess
import distutils.spawn
from . import cr_exceptions

ACTIVE_PORTS = set()

class ChromeExecutionManager():
	"""
	Class for managing talking to a chromium instance, as well as
	managing the chromium instance's execution lifetime.

	"""

	def __init__(self,
			binary,
			base_tab_key,
			host              = 'localhost',
			port              = None,
			websocket_timeout = 10,
			):
		"""

		Start up a chromium instance binary `binary`, with the base debug port `port`,
		on host `localhost`.

		Note: If the port is set to None (the default), the port will be the next availble port accounting for other
		chrome instances in the current python process.

		If port is not none, and the port is already in use, you will get a ReusedPortError.

		base_tab_key is any hashable python object that is used for multi-tab interfacing.

		"""

		if port is None:
			port = 9222
			while port in ACTIVE_PORTS:
				port += 1

		if port in ACTIVE_PORTS:
			raise cr_exceptions.ReusedPortError("Attempting to start chromium using a already-in-use debug port (%s, %s)!" % (port, ACTIVE_PORTS))

		ACTIVE_PORTS.add(port)

		self.binary = binary
		self.host = host
		self.port = port
		self.msg_id = 0
		self.websocket_timeout = websocket_timeout

		self.tablist = None
		self.soclist = {}
		self.tab_id_map = {}

		self.log = logging.getLogger("Main.ChromeController.ExecutionManager")


		self.log.info("Launching binary %s", self.binary)
		self._launch_process(self.binary, self.port, base_tab_key)

		# self.log.info("Connecting to %s:%s", self.host, self.port)
		# self.connect(base_tab_key)


		self.messages = []

	def _launch_process(self, binary, dbg_port, base_tab_key):

		if binary is None:
			binary = "chromium"
		if not os.path.exists(binary):
			fixed = distutils.spawn.find_executable(binary)
			if fixed:
				binary = fixed
		if not binary or not os.path.exists(binary):
			raise RuntimeError("Could not find binary '%s'" % binary)

		argv = [
				binary,
				'--headless',
				'--disable-gpu',
				'--remote-debugging-port={dbg_port}'.format(dbg_port=dbg_port)
			]

		self.cr_proc = subprocess.Popen(argv,
										stdin=open(os.path.devnull, "r"),
										stdout=subprocess.PIPE,
										stderr=subprocess.PIPE)

		self.log.debug("Spawned process: %s, PID: %s", self.cr_proc, self.cr_proc.pid)
		for x in range(10):
			try:
				self.tablist = self.fetch_tablist()

				if not self.tablist:
					raise cr_exceptions.ChromeStartupException("No tabs in started chromium?")

				# self.tab_id_map[base_tab_key] = self.tablist[0]['id']
				# print("Tab base key:", base_tab_key, self.tablist[0]['id'])
				return
			except cr_exceptions.ChromeConnectFailure as e:
				if x > 8:
					raise e
				time.sleep(1)

	def close_chromium(self):
		'''
		Close the remote chromium instance.

		This command is normally executed as part of the class destructor.
		It can be called early without issue, but calling ANY class functions
		after the remote chromium instance is shut down will have unknown effects.

		Note that if you are rapidly creating and destroying ChromeController instances,
		you may need to *explicitly* call this before destruction.
		'''
		if self.cr_proc:
			self.log.debug("Sending sigint to chromium")
			self.cr_proc.send_signal(signal.SIGINT)
			self.log.debug("Waiting for chromium to exit")
			self.cr_proc.wait(timeout=5)
			try:
				self.cr_proc.terminate()
			except ProcessLookupError:
				self.log.debug("Process exited normally, no need to terminate.")
			self.log.debug("Pid: %s, Return code: %s", self.cr_proc.pid, self.cr_proc.returncode)
			self.log.debug("Chromium closed!")

		ACTIVE_PORTS.discard(self.port)


	def check_process_ded(self):
		self.cr_proc.poll()
		if self.cr_proc.returncode != None:
			stdout, stderr = self.cr_proc.communicate()
			raise cr_exceptions.ChromeError("Chromium process died unexpectedly! Don't know how to continue!\n	Chromium stdout: {}\n	Chromium stderr: {}".format(stdout.decode("utf-8"), stderr.decode("utf-8")))

	def _get_tab_idx_for_key(self, tab_key):

		if tab_key not in self.tab_id_map:
			return None

		cr_tab_id = self.tab_id_map[tab_key]

		for idx, tab_dat in enumerate(self.tablist):
			if tab_dat['id'] == cr_tab_id:
				return idx

		self.log.error("Missing tab %s from tab list!", tab_key)
		self.log.error("Corresponding cr tab id: %s!", cr_tab_id)
		for tab_key, cr_tab_id in self.tab_id_map.items():
			self.log.error("Tab pair %s -> %s", tab_key, cr_tab_id)
		self.log.error("Chromium tabs (%s):", len(self.tablist))
		for tab in self.tablist:
			self.log.error("	cr tab id: %s", tab['id'])

		raise cr_exceptions.ChromeTabNotFoundError("Tab with ID %s (cr ID: %s) not found!" % (tab_key, cr_tab_id))

	def connect(self, tab_key):
		"""
		Open a websocket connection to remote browser, determined by
		self.host and self.port.  Each tab has it's own websocket
		endpoint.

		"""

		assert self.tablist is not None

		tab_idx = self._get_tab_idx_for_key(tab_key)

		if not self.tablist:
			self.tablist = self.fetch_tablist()

		for fails in range(9999):
			try:
				# If we're one past the end of the tablist, we need to create a new tab
				if tab_idx is None:
					self.log.debug("Creating new tab (%s active)", len(self.tablist))
					self.__create_new_tab(tab_key)

				self.__connect_to_tab(tab_key)
				break

			except cr_exceptions.ChromeConnectFailure as e:
				if fails > 6:
					self.log.error("Failed to fetch tab websocket URL after %s retries. Aborting!", fails)
					raise e
				self.log.info("Tab may not have started yet (%s tabs active). Recreating.", len(self.tablist))
				# self.log.info("Tag: %s", self.tablist[tab_idx])


				# For reasons I don't understand, sometimes a new tab doesn't get a websocket
				# debugger URL. Anyways, we close and re-open the tab if that happens.
				# TODO: Handle the case when this happens on the first tab. I think closing the first
				#       tab will kill chromium.
				self.__close_tab(tab_key)

	def __repr__(self):
		return "<ChromeExecutionManager for tabs \n%s\n>" % pprint.pformat(self.tab_id_map)

	def pprint_tablist(self):
		self.log.info("Tablist type: %s", type(self.tablist))
		for line in pprint.pformat(list(enumerate(self.tablist))).split("\n"):
			self.log.info("	%s", line)

	def __create_new_tab(self, tab_key, start_at_url=None):
		url = "http://%s:%s/json/new" % (self.host, self.port)
		if start_at_url:
			url += "?%s" % (start_at_url, )
		try:
			requests.get(url)
			self.tablist = self.fetch_tablist()
			known_tabs = list(self.tab_id_map.values())
			for tab in self.tablist:

				# Glom onto the first tab in the tablist that's not known.
				if tab['id'] not in known_tabs:
					self.tab_id_map[tab_key] = tab['id']
					return


		except requests.exceptions.ConnectionError:
			raise cr_exceptions.ChromeConnectFailure("Failed to create a new tab in remote chromium!")

	def __connect_to_tab(self, tab_key):
		assert tab_key not in self.soclist

		tab_idx = self._get_tab_idx_for_key(tab_key)

		if not 'webSocketDebuggerUrl' in self.tablist[-1]:
			raise cr_exceptions.ChromeConnectFailure("Tab %s has no 'webSocketDebuggerUrl' (%s)" % (tab_idx, self.tablist))

		wsurl = self.tablist[tab_idx]['webSocketDebuggerUrl']

		try:
			self.log.info("Setting up websocket connection")
			self.soclist[tab_key] = websocket.create_connection(wsurl)
			self.soclist[tab_key].settimeout(self.websocket_timeout)

		except (socket.timeout, websocket.WebSocketTimeoutException):
			raise cr_exceptions.ChromeCommunicationsError("Could not connect to remote chromium.")

	def __close_tab(self, tab_key, timeout=None):

		# traceback.print_stack()

		cr_tab_id = self.tab_id_map[tab_key]

		url = "http://%s:%s/json/close/%s" % (self.host, self.port, cr_tab_id)
		requests.get(url, timeout=timeout)

		# Delete the now-removed tab from the tab map
		self.tab_id_map.pop(tab_key)

		self.log.info("Closing websocket connecton %s (%s)", tab_key, len(self.soclist))
		self.soclist.pop(tab_key, None)

		self.tablist = self.fetch_tablist()
		return self.tablist

	def close_tab(self, tab_key):
		self.log.info("Closing tab %s (cr ID: %s)", tab_key, self.tab_id_map[tab_key])
		self.__close_tab(tab_key)

		# If we've closed all the chrome tabs, shut down the interface.
		if len(self.tab_id_map) == 0:
			self.log.info("All tabs are closed. Closing chromium!")
			self.close_websockets()
			self.close_chromium()

	def close_websockets(self):
		""" Close websocket connection to remote browser."""
		self.log.info("Websocket Teardown called")
		for key in list(self.soclist.keys()):
			if self.soclist[key]:
				self.soclist[key].close()
			self.soclist.pop(key)


	def fetch_tablist(self):
		"""Connect to host:port and request list of tabs
			 return list of dicts of data about open tabs."""
		# find websocket endpoint
		try:
			response = requests.get("http://%s:%s/json" % (self.host, self.port))
		except requests.exceptions.ConnectionError:
			raise cr_exceptions.ChromeConnectFailure("Failed to fetch configuration json from browser!")

		tablist = json.loads(response.text)

		return tablist

	def __check_open_socket(self, tab_key):
		# self.log.info("__check_open_socket -> %s", tab_key)
		if not tab_key in self.soclist:
			self.connect(tab_key=tab_key)
		if self.soclist[tab_key].connected is not True:
			self.connect(tab_key=tab_key)


	def synchronous_command(self, command, tab_key, **params):
		"""
		Synchronously execute command `command` with params `params` in the
		remote chrome instance, returning the response from the chrome instance.

		"""
		self.log.debug("Synchronous_command to tab %s (%s):", tab_key, self._get_tab_idx_for_key(tab_key))
		self.log.debug("	command: '%s'", command)
		self.log.debug("	params:  '%s'", params)
		self.log.debug("	tab_key:  '%s'", tab_key)

		send_id = self.send(command=command, tab_key=tab_key, params=params)
		resp = self.recv(message_id=send_id, tab_key=tab_key)

		self.log.debug("	Response: '%s'", str(resp).encode("ascii", 'ignore').decode("ascii"))

		# self.log.debug("	resolved tab idx %s:", self.tab_id_map[tab_key])
		return resp

	def send(self, command, tab_key, params=None):
		'''
		Send command `command` with optional parameters `params` to the
		remote chrome instance.

		The command `id` is automatically added to the outgoing message.

		return value is the command id, which can be used to match a command
		to it's associated response.
		'''
		self.__check_open_socket(tab_key)

		sent_id = self.msg_id

		command = {
				"id": self.msg_id,
				"method": command,
			}

		if params:
			command["params"] = params
		navcom = json.dumps(command)


		# self.log.debug("		Sending: '%s'", navcom)
		try:
			self.soclist[tab_key].send(navcom)
		except (socket.timeout, websocket.WebSocketTimeoutException):
			raise cr_exceptions.ChromeCommunicationsError("Failure sending command to chromium.")
		except websocket.WebSocketConnectionClosedException:
			raise cr_exceptions.ChromeCommunicationsError("Websocket appears to have been closed. Is the"
				" remote chromium instance dead?")


		self.msg_id += 1
		return sent_id


	def ___recv(self, tab_key):
		try:
			tmp = self.soclist[tab_key].recv()
			self.log.debug("		Received: '%s'", tmp)

			decoded = json.loads(tmp)
			return decoded
		except (socket.timeout, websocket.WebSocketTimeoutException):
			return None
		except websocket.WebSocketConnectionClosedException:
			raise cr_exceptions.ChromeCommunicationsError("Websocket appears to have been closed. Is the"
				" remote chromium instance dead?")

	def recv_filtered(self, keycheck, tab_key, timeout=30):
		'''
		Receive a filtered message, using the callable `keycheck` to filter received messages
		for content.

		`keycheck` is expected to be a callable that takes a single parameter (the decoded response
		from chromium), and returns a boolean (true, if the command is the one filtered for, or false
		if the command is not the one filtered for).

		This is used internally, for example, by `recv()`, to filter the response for a specific ID:

		```
			def check_func(message):
				if message_id is None:
					return True
				if "id" in message:
					return message['id'] == message_id
				return False
			return self.recv_filtered(check_func, timeout)

		```

		Note that the function is defined dynamically, and `message_id` is captured via closure.

		'''


		self.__check_open_socket(tab_key)

		# First, check if the message has already been received.
		for idx in range(len(self.messages)):
			if keycheck(self.messages[idx]):
				return self.messages.pop(idx)

		timeout_at = time.time() + timeout
		while 1:
			tmp = self.___recv(tab_key)
			if keycheck(tmp):
				return tmp
			else:
				self.messages.append(tmp)

			if time.time() > timeout_at:
				return None


	def recv(self, tab_key, message_id=None, timeout=30):
		'''
		Recieve a message, optionally filtering for a specified message id.

		If `message_id` is none, the first command in the receive queue is returned.
		If `message_id` is not none, the command waits untill a message is received with
		the specified id, or it times out.

		Timeout is the number of seconds to wait for a response, or `None` if the timeout
		has expired with no response.

		'''

		self.__check_open_socket(tab_key)

		# First, check if the message has already been received.
		for idx in range(len(self.messages)):
			if self.messages[idx]:
				if "id" in self.messages[idx] and message_id:
					if self.messages[idx]['id'] == message_id:
						return self.messages.pop(idx)

		# Then spin untill we either have the message,
		# or have timed out.
		def check_func(message):
			if message_id is None:
				return True
			if not message:
				self.log.debug("Message is not true (%s)!", message)
				return False
			if "id" in message:
				return message['id'] == message_id
			return False

		return self.recv_filtered(check_func, tab_key, timeout)


	def drain(self, tab_key):
		'''
		Return all messages in waiting for the websocket connection.
		'''

		ret = []
		while len(self.messages):
			ret.append(self.messages.pop(0))

		tmp = self.___recv(tab_key)
		while tmp is not None:
			ret.append(tmp)
			tmp = self.___recv(tab_key)
		return ret

	def __del__(self):
		self.log.debug("Transport destructor called. Tearing down chromium")
		try:
			self.close_chromium()
		except Exception:
			pass

		ACTIVE_PORTS.discard(self.port)

if __name__ == '__main__':
	import doctest
	doctest.testmod()

