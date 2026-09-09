import json

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, key: str):
        self.cipher = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def pack(self, value: dict) -> str:
        return self.encrypt(json.dumps(value, ensure_ascii=False))

    def unpack(self, value: str) -> dict:
        return json.loads(self.decrypt(value))
