from ..config import RAW_DIR, CHROMA_DIR, SQLITE_PATH, ACTION_PATH
from ..data.ingest import build_documents, load_workbook
from ..data.repository import Repository
from ..tools.documents import DocumentTool
from ..tools.actions import ActionTool

class Runtime:
    def __init__(self):
        wb=load_workbook(RAW_DIR, SQLITE_PATH); docs=build_documents(RAW_DIR, CHROMA_DIR)
        self.dataset_now=wb["dataset_now"]; self.docs=docs; self.repo=Repository(SQLITE_PATH,self.dataset_now); self.documents=DocumentTool(docs,CHROMA_DIR); self.actions=ActionTool(ACTION_PATH)
