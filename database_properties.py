import psycopg2

class Properties:
    def __init__(self, db_addr: str, db_port: int, db_name: str, db_user: str, db_pass: str):
        self.db_addr = db_addr
        self.db_port = db_port
        self.db_name = db_name
        self.db_user = db_user
        self.db_pass = db_pass
        self._conn = None

    @property
    def conn_string(self) -> str:
        return f'host={self.db_addr} port={self.db_port} dbname={self.db_name}' \
                f' user={self.db_user} password={self.db_pass}'
    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.conn_string)
        return self._conn
    
    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()