"""Run the recovery protocol tests against the installed native FAISS build."""
import unittest
from unittest.mock import patch
from g2h import semantic_index
from test_index_snapshots import SnapshotTests


class CountingFaiss:
    def __init__(self, native):self.native=native;self.reads=0;self.writes=0
    def __getattr__(self,name):return getattr(self.native,name)
    def write_index(self,index,path):self.writes+=1;return self.native.write_index(index,path)
    def read_index(self,path):self.reads+=1;return self.native.read_index(path)


class NativeSnapshotTests(SnapshotTests):
    def setUp(self):
        try:
            native=semantic_index.faiss_module()
        except RuntimeError as exc:
            if not str(exc).startswith('FAISS is required;'):
                raise
            self.skipTest('FAISS not installed in this test environment')
        super().setUp()
        self.patches[0].stop()
        self.fake=CountingFaiss(native)
        self.patches[0]=patch.object(semantic_index,'faiss_module',return_value=self.fake)
        self.patches[0].start()


if __name__=='__main__':unittest.main()
