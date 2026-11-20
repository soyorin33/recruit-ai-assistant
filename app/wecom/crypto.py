"""企业微信回调消息加解密（WXBizMsgCrypt 的 Python 实现，AES-256-CBC + PKCS#7(32)）。

官方文档：https://developer.work.weixin.qq.com/document/path/90968
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK = 32


class WeComCryptoError(Exception):
    pass


class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise WeComCryptoError("EncodingAESKey 长度不正确，应为 43 位")
        self.iv = self.key[:16]
        self.receive_id = receive_id

    # ---------- 签名 ----------
    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, str(timestamp), str(nonce), encrypt])
        return hashlib.sha1("".join(items).encode()).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        if self.signature(timestamp, nonce, encrypt) != msg_signature:
            raise WeComCryptoError("签名校验失败")

    # ---------- AES ----------
    def encrypt(self, plaintext: str, random16: bytes | None = None) -> str:
        msg = plaintext.encode()
        raw = (random16 or os.urandom(16)) + struct.pack("I", socket.htonl(len(msg))) + msg + self.receive_id.encode()
        pad = BLOCK - len(raw) % BLOCK
        raw += bytes([pad]) * pad
        enc = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(enc.update(raw) + enc.finalize()).decode()

    def decrypt(self, encrypted: str) -> str:
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        raw = dec.update(base64.b64decode(encrypted)) + dec.finalize()
        pad = raw[-1]
        if not 1 <= pad <= BLOCK:
            raise WeComCryptoError("解密填充错误")
        raw = raw[:-pad]
        length = socket.ntohl(struct.unpack("I", raw[16:20])[0])
        msg = raw[20:20 + length]
        receive_id = raw[20 + length:].decode()
        if receive_id != self.receive_id:
            raise WeComCryptoError("ReceiveId 不匹配")
        return msg.decode()

    # ---------- 业务封装 ----------
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """配置回调 URL 时企微发起的 GET 校验，返回明文 echostr。"""
        self.verify(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def decrypt_xml(self, msg_signature: str, timestamp: str, nonce: str, body: bytes) -> dict:
        """解密 POST 回调，返回消息字段 dict（ToUserName/FromUserName/MsgType/Content/MediaId...）。"""
        encrypt = ET.fromstring(body).findtext("Encrypt") or ""
        self.verify(msg_signature, timestamp, nonce, encrypt)
        root = ET.fromstring(self.decrypt(encrypt))
        return {child.tag: (child.text or "") for child in root}

    def encrypt_xml(self, plaintext_xml: str, timestamp: str, nonce: str) -> str:
        encrypt = self.encrypt(plaintext_xml)
        sig = self.signature(timestamp, nonce, encrypt)
        return (f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt><MsgSignature><![CDATA[{sig}]]></MsgSignature>"
                f"<TimeStamp>{timestamp}</TimeStamp><Nonce><![CDATA[{nonce}]]></Nonce></xml>")
