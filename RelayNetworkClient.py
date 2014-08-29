#!/usr/bin/env python
#
# RelayNetworkClient.py
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#
import socket, sys, time, struct
from struct import pack, unpack, unpack_from
from threading import Timer, Lock, RLock
from hashlib import sha256
try:
	from collections import OrderedDict # Python 3.1
except ImportError:
	from ordereddict import OrderedDict # PIP
try:
	from bitcoin.core import CBlockHeader, CBlock, CTransaction, b2lx
	deserialize_utils = True
except ImportError:
	deserialize_utils = False


class ProtocolError(Exception):
	pass


class FlaggedArraySet:
	def __init__(self, max_size):
		self.max_size = max_size
		self.indexes_removed = set()
		self.backing_dict = OrderedDict()
		self.backing_reverse_dict = {}
		self.offset = 0
		self.total = 0
		self.flag_count = 0

	def len(self):
		return len(self.backing_dict)

	def get_flag_count(self):
		return self.flag_count

	def contains(self, e):
		return (e, False) in self.backing_dict or (e, True) in self.backing_dict

	def removed_from_backing_dict(self, item_index_pair):
		del self.backing_reverse_dict[item_index_pair[1]]
		if item_index_pair[0][1]:
			self.flag_count -= 1

		if self.offset != item_index_pair[1]:
			for i in range(item_index_pair[1] - 1, self.offset - 1, -1):
				e = self.backing_reverse_dict[i]
				del self.backing_reverse_dict[i]
				self.backing_dict[e] = i+1
				self.backing_reverse_dict[i+1] = e
		self.offset += 1

	def add(self, e, flag):
		if self.contains(e):
			return

		while self.len() >= self.max_size:
			self.removed_from_backing_dict(self.backing_dict.popitem(last=False))

		self.backing_dict[(e, flag)] = self.total
		self.backing_reverse_dict[self.total] = (e, flag)
		self.total += 1
		if flag:
			self.flalg_count += 1

	def remove(self, e):
		if (e, False) in self.backing_dict:
			index = self.backing_dict[(e, False)]
			del self.backing_dict[(e, False)]
			self.removed_from_backing_dict(((e, False), index))
		elif (e, True) in self.backing_dict:
			index = self.backing_dict[(e, True)]
			del self.backing_dict[(e, True)]
			self.removed_from_backing_dict(((e, True), index))

	def get_index(self, e):
		if (e, False) in self.backing_dict:
			return self.backing_dict[(e, False)] - self.offset
		elif (e, True) in self.backing_dict:
			return self.backing_dict[(e, True)] - self.offset
		else:
			return None

	def get_by_index(self, index):
		if index + self.offset in self.backing_reverse_dict:
			return self.backing_reverse_dict[index + self.offset][0]
		else:
			return None

def decode_varint(data, offset):
	if unpack_from('<B', data, offset)[0] < 0xfd:
		return unpack_from('<B', data, offset + 0)[0], offset + 1
	elif data[offset] == 0xfd:
		return unpack_from('<H', data, offset + 1)[0], offset + 3
	elif data[offset] == 0xfe:
		return unpack_from('<I', data, offset + 1)[0], offset + 5
	else:
		return unpack_from('<Q', data, offset + 1)[0], offset + 9

class RelayNetworkClient:
	MAGIC_BYTES = int(0xF2BEEF42)
	VERSION_TYPE, BLOCK_TYPE, TRANSACTION_TYPE, END_BLOCK_TYPE, MAX_VERSION_TYPE = 0, 1, 2, 3, 4
	VERSION_STRING = b'prioritized panther'
	MAX_RELAY_TRANSACTION_BYTES = 10000
	MAX_RELAY_OVERSIZE_TRANSACTION_BYTES = 250000
	MAX_EXTRA_OVERSIZE_TRANSACTIONS = 20

	def __init__(self, server, data_recipient):
		self.server = server
		self.data_recipient = data_recipient
		self.send_lock = Lock()
		self.reconnect()

	def reconnect(self):
		t = Timer(1, self.connect) # becomes the message-processing thread
		t.daemon = True
		t.start()

	def connect(self):
		self.send_lock.acquire()
		self.recv_transaction_cache = FlaggedArraySet(1000)
		self.send_transaction_cache = FlaggedArraySet(1000)
		self.relay_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.relay_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		try:
			try:
				self.relay_sock.connect((self.server, 8336))
				self.relay_sock.sendall(pack('>3I', self.MAGIC_BYTES, self.VERSION_TYPE, len(self.VERSION_STRING)))
				self.relay_sock.sendall(self.VERSION_STRING)
			finally:
				self.send_lock.release()

			while True:
				msg_header = unpack('>3I', self.relay_sock.recv(3 * 4, socket.MSG_WAITALL))
				if msg_header[0] != self.MAGIC_BYTES:
					raise ProtocolError("Invalid magic bytes: " + str(msg_header[0]) + " != " +  str(self.MAGIC_BYTES))
				if msg_header[2] > 1000000:
					raise ProtocolError("Got message too large: " + str(msg_header[2]))

				if msg_header[1] == self.VERSION_TYPE:
					version = self.relay_sock.recv(msg_header[2], socket.MSG_WAITALL)
					if version != self.VERSION_STRING:
						raise ProtocolError("Got back unknown version type " + str(version))
					print("Connected to relay node with protocol version " + str(version))
				elif msg_header[1] == self.BLOCK_TYPE:
					if msg_header[2] > 10000:
						raise ProtocolError("Got a BLOCK message with far too many transactions: " + str(msg_header[2]))

					wire_bytes = 3 * 4

					header_data = self.relay_sock.recv(80, socket.MSG_WAITALL)
					wire_bytes += 80
					self.data_recipient.provide_block_header(header_data)
					if deserialize_utils:
						header = CBlockHeader.deserialize(header_data)
						print("Got block header: " + str(b2lx(header.GetHash())))

					if msg_header[2] < 0xfd:
						block_data = header_data + pack('B', msg_header[2])
					elif msg_header[2] < 0xffff:
						block_data = header_data + b'\xfd' + pack('<H', msg_header[2])
					elif msg_header[2] < 0xffffffff:
						block_data = header_data + b'\xfe' + pack('<I', msg_header[2])
					else:
						raise ProtocolError("WTF?????")

					for i in range(0, msg_header[2]):
						index = unpack('>H', self.relay_sock.recv(2, socket.MSG_WAITALL))[0]
						wire_bytes += 2
						if index == 0xffff:
							data_length = unpack('>HB', self.relay_sock.recv(3, socket.MSG_WAITALL))
							wire_bytes += 3
							data_length = data_length[0] << 8 | data_length[1]
							if data_length > 1000000:
								raise ProtocolError("Got in-block transaction of size > MAX_BLOCK_SIZE: " + str(dat_length))
							transaction_data = self.relay_sock.recv(data_length, socket.MSG_WAITALL)
							wire_bytes += data_length
							if deserialize_utils:
								transaction = CTransaction.deserialize(transaction_data)
								print("Got in-block full transaction: " + str(b2lx(transaction.GetHash())) + " of length " + str(data_length))
							else:
								print("Got in-block full transaction of length " + str(data_length))
							block_data += transaction_data
						else:
							transaction_data = self.recv_transaction_cache.get_by_index(index)
							if transaction_data is None:
								raise ProtocolError("Got index for a transaction we didn't have")
							self.recv_transaction_cache.remove(transaction_data)
							block_data += transaction_data

					self.data_recipient.provide_block(block_data)

					if deserialize_utils:
						print("Got full block " + str(b2lx(header.GetHash())) + " with " + str(msg_header[2]) + " transactions in " + str(wire_bytes) + " wire bytes")
						block = CBlock.deserialize(block_data)
						print("Deserialized full block " + str(b2lx(block.GetHash())))
					else:
						print("Got full block with " + str(msg_header[2]) + " transactions in " + str(wire_bytes) + " wire bytes")

					if unpack('>3I', self.relay_sock.recv(3 * 4, socket.MSG_WAITALL)) != (self.MAGIC_BYTES, self.END_BLOCK_TYPE, 0):
						raise ProtocolError("Invalid END_BLOCK message after block")

				elif msg_header[1] == self.TRANSACTION_TYPE:
					if msg_header[2] > self.MAX_RELAY_TRANSACTION_BYTES and (self.recv_transaction_cache.get_flag_count() >= self.MAX_EXTRA_OVERSIZE_TRANSACTIONS or msg_header[2] > self.MAX_RELAY_OVERSIZE_TRANSACTION_BYTES):
						raise ProtocolError("Got a freely relayed transaction too large (" + str(msg_header[2]) + ") bytes")
					transaction_data = self.relay_sock.recv(msg_header[2], socket.MSG_WAITALL)
					self.recv_transaction_cache.add(transaction_data, msg_header[2] > self.MAX_RELAY_OVERSIZE_TRANSACTION_BYTES)

					self.data_recipient.provide_transaction(transaction_data)

					if deserialize_utils:
						transaction = CTransaction.deserialize(transaction_data)
						print("Got transaction: " + str(b2lx(transaction.GetHash())))
					else:
						print("Got transaction of length " + str(msg_header[2]))

				elif msg_header[1] == self.MAX_VERSION_TYPE:
					version = self.relay_sock.recv(msg_header[2], socket.MSG_WAITALL)
					print("Relay network now uses version " + str(version) + " (PLEASE UPGRADE)")

				else:
					raise ProtocolError("Unknown message type: " + str(msg_header[1]))

		except (OSError, socket.error) as err:
			print("Lost connect to relay node:", err)
			self.reconnect()
		except ProtocolError as err:
			print("Error processing data from relay node:", err)
			self.reconnect()
		except Exception as err:
			print("Unknown error processing data from relay node:", err)
			self.reconnect()

	def provide_transaction(self, transaction_data):
		self.send_lock.acquire()

		if self.send_transaction_cache.contains(transaction_data):
			self.send_lock.release()
			return
		if len(transaction_data) > self.MAX_RELAY_TRANSACTION_BYTES and (len(transaction_data) > self.MAX_RELAY_OVERSIZE_TRANSACTION_BYTES or self.send_transaction_cache.get_flag_count() >= MAX_EXTRA_OVERSIZE_TRANSACTIONS):
			self.send_lock.release()
			return

		try:
			relay_data = pack('>3I', self.MAGIC_BYTES, self.TRANSACTION_TYPE, len(transaction_data))
			relay_data += transaction_data
			self.relay_sock.sendall(relay_data)
			self.send_transaction_cache.add(transaction_data, len(transaction_data) > self.MAX_RELAY_OVERSIZE_TRANSACTION_BYTES)

			if deserialize_utils:
				transaction = CTransaction.deserialize(transaction_data)
				print("Sent transaction " + str(b2lx(transaction.GetHash())) + " of size " + str(len(transaction_data)))
			else:
				print("Sent transaction of size " + str(len(transaction_data)))
		except (OSError, socket.error) as err:
			print("Failed to send to relay node: ", err)

		self.send_lock.release()

	def provide_block_header(self, header_data):
		return

	def provide_block(self, block_data):
		"""THIS METHOD WILL BLOCK UNTIL SENDING IS COMPLETE"""
		tx_count, read_pos = decode_varint(block_data, 80)
		self.send_lock.acquire()
		try:
			relay_data = pack('>3I', self.MAGIC_BYTES, self.BLOCK_TYPE, tx_count) + block_data[0:80]
			wire_bytes = 3 * 4 + 80
			for i in range(0, tx_count):
				tx_start = read_pos
				read_pos += 4

				tx_in_count, read_pos = decode_varint(block_data, read_pos)
				for j in range(0, tx_in_count):
					read_pos += 36
					script_len, read_pos = decode_varint(block_data, read_pos)
					read_pos += script_len + 4

				tx_out_count, read_pos = decode_varint(block_data, read_pos)
				for j in range(0, tx_out_count):
					read_pos += 8
					script_len, read_pos = decode_varint(block_data, read_pos)
					read_pos += script_len

				read_pos += 4

				transaction_data = block_data[tx_start:read_pos]
				tx_index = self.send_transaction_cache.get_index(transaction_data)
				if tx_index is None:
					relay_data += pack('>H', 0xffff) + pack('>HB', len(transaction_data) >> 8, len(transaction_data) & 0xff) + transaction_data
					wire_bytes += 2 + 3 + len(transaction_data)
				else:
					relay_data += pack('>H', tx_index)
					wire_bytes += 2
					self.send_transaction_cache.remove(transaction_data)

			relay_data += pack('>3I', self.MAGIC_BYTES, self.END_BLOCK_TYPE, 0)
			self.relay_sock.sendall(relay_data)

			if deserialize_utils:
				block = CBlock.deserialize(block_data)
				print("Sent block " + str(b2lx(block.GetHash())) + " of size " + str(len(block_data)) + " with " + str(wire_bytes) + " bytes on the wire")
			else:
				print("Sent block of size " + str(len(block_data)) + " with " + str(wire_bytes) + " bytes on the wire")
		except (OSError, socket.error) as err:
			print("Failed to send to relay node: ", err)
		finally:
			self.send_lock.release()

import traceback
class BitcoinP2PRelayer:
	MSG_TX = 1
	MSG_BLOCK = 2

	def __init__(self, server, data_recipient):
		self.server = server
		self.data_recipient = data_recipient
		self.send_lock = RLock()
		self.reconnect()

	def reconnect(self):
		t = Timer(1, self.connect) # becomes the message-processing thread
		t.daemon = True
		t.start()

	def send_message(self, command, data):
		self.send_lock.acquire()
		try:
			checksum = sha256(sha256(data).digest()).digest()[0:4]
			message = pack('<4s12sI4s', b'\xf9\xbe\xb4\xd9', command, len(data), checksum)
			message += data
			self.sock.sendall(message)
		finally:
			self.send_lock.release()

	def make_dummy_address(self):
		return pack('<Q16sH', 0, b'', 0)

	def connect(self):
		self.send_lock.acquire()
		self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		try:
			try:
				self.sock.connect(self.server)
				self.send_message(b'version', pack('<iQq26s26sQ1si', 70000, 0, int(time.time()), self.make_dummy_address(), self.make_dummy_address(), 42, b'', 0))
			finally:
				self.send_lock.release()

			while True:
				magic, command, data_len, checksum = unpack('<4s12sI4s', self.sock.recv(4 * 3 + 12, socket.MSG_WAITALL))
				if magic != b'\xf9\xbe\xb4\xd9':
					raise ProtocolError("Invalid protocol magic")
				command = command[0:command.index(b'\00')]

				if data_len > 5000000:
					raise ProtocolError("Got message > 5M")
				data = self.sock.recv(data_len, socket.MSG_WAITALL)
				if checksum != sha256(sha256(data).digest()).digest()[0:4]:
					raise ProtocolError("Invalid message checksum")

				if command == b'version':
					print("Connected to bitcoind with protocol version " + str(unpack('<i', data[0:4])[0]))
					self.send_message(b'verack', b'')
				elif command == b'verack':
					print("Finished connect handshake with bitcoind")
				elif command == b'ping':
					self.send_message(b'pong', data)
				elif command == b'inv':
					self.send_message(b'getdata', data)
				elif command == b'block':
					self.data_recipient.provide_block(data)
				elif command == b'tx':
					self.data_recipient.provide_transaction(data)

		except (OSError, socket.error, struct.error) as err:
			print("Lost connect to bitcoind node:", err)
			self.reconnect()
		except ProtocolError as err:
			print("Error processing data from bitcoind node:", err)
			self.reconnect()
		except Exception as err:
			traceback.print_exc()
			print("Unknown error processing data from bitcoind node:", err)
			self.reconnect()

	def provide_block_header(self, header_data):
		try:
			self.send_message(b'inv', b'\x01\x02\x00\x00\x00' + sha256(sha256(header_data).digest()).digest())
		except (OSError, socket.error) as err:
			print("Failed to send transaction to bitcoind node: ", err)

	def provide_block(self, block_data):
		try:
			self.send_message(b'block', block_data)
		except (OSError, socket.error) as err:
			print("Failed to send block to bitcoind node: ", err)

	def provide_transaction(self, transaction_data):
		try:
			self.send_message(b'tx', transaction_data)
		except (OSError, socket.error) as err:
			print("Failed to send transaction to bitcoind node: ", err)


class RelayNetworkToP2PManager:
	def __init__(self, relay_server, p2p_server):
		relay = RelayNetworkClient(relay_server, self)
		self.p2p = BitcoinP2PRelayer(p2p_server, relay)

	def provide_block_header(self, header_data):
		self.p2p.provide_block_header(header_data)

	def provide_block(self, block_data):
		self.p2p.provide_block(block_data)

	def provide_transaction(self, transaction_data):
		self.p2p.provide_transaction(transaction_data)

if __name__ == "__main__":
	if len(sys.argv) != 4:
		print("USAGE: ", sys.argv[0], " RELAY_NODE.relay.mattcorallo.com LOCAL_BITCOIND LOCAL_BITCOIND_PORT")
		sys.exit(1)
	RelayNetworkToP2PManager(sys.argv[1], (sys.argv[2], int(sys.argv[3])))
	while True:
		time.sleep(60 * 60 * 24)
