from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import os
import uuid
import shutil
from pathlib import Path
import tempfile

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import FAISS
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory

app = FastAPI(title="PDF RAG MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("./uploads")
VECTORSTORE_DIR = Path("./vectorstores")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "your-api-key")

UPLOAD_DIR.mkdir(exist_ok=True)
VECTORSTORE_DIR.mkdir(exist_ok=True)

conversation_chains = {}

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    answer: str
    sources: List[Dict[str, str]]

embeddings = OpenAIEmbeddings(openai_api_key=OPENAI_API_KEY)

def process_pdf(file_path: Path, session_id: str):
    try:
        loader = PyPDFLoader(str(file_path))
        documents = loader.load()

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )
        chunks = text_splitter.split_documents(documents)
        
        vectorstore = FAISS.from_documents(chunks, embeddings)
        
        vector_store_path = VECTORSTORE_DIR / session_id
        vectorstore.save_local(str(vector_store_path))
        
        llm = ChatOpenAI(
            temperature=0,
            model_name="gpt-3.5-turbo",
            openai_api_key=OPENAI_API_KEY
        )
        
        memory = ConversationBufferMemory(
            memory_key="chat_history",
            output_key="answer",
            return_messages=True
        )
        
        conversation_chain = ConversationalRetrievalChain.from_llm(
            llm=llm,
            retriever=vectorstore.as_retriever(search_kwargs={"k": 3}),
            memory=memory,
            return_source_documents=True,
            verbose=True
        )

        conversation_chains[session_id] = conversation_chain
        
        return True
    except Exception as e:
        print(f"Error processing PDF: {e}")
        return False

@app.post("/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    try:
        session_id = str(uuid.uuid4())
        
        file_path = UPLOAD_DIR / f"{session_id}_{file.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        background_tasks.add_task(process_pdf, file_path, session_id)
        
        return {"session_id": session_id, "message": "File uploaded. Processing started."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        if request.session_id not in conversation_chains:

            vector_store_path = VECTORSTORE_DIR / request.session_id
            if vector_store_path.exists():
                vectorstore = FAISS.load_local(str(vector_store_path), embeddings)
                
                llm = ChatOpenAI(
                    temperature=0,
                    model_name="gpt-3.5-turbo",
                    openai_api_key=OPENAI_API_KEY
                )
                
                memory = ConversationBufferMemory(
                    memory_key="chat_history",
                    output_key="answer",
                    return_messages=True
                )
                
                conversation_chain = ConversationalRetrievalChain.from_llm(
                    llm=llm,
                    retriever=vectorstore.as_retriever(search_kwargs={"k": 3}),
                    memory=memory,
                    return_source_documents=True
                )
                
                conversation_chains[request.session_id] = conversation_chain
            else:
                raise HTTPException(status_code=404, detail="Session not found or still processing")
        
        conversation_chain = conversation_chains[request.session_id]
        
        try:
            result = conversation_chain({"question": request.message})

            sources = []
            for doc in result.get("source_documents", []):
                source = {
                    "page": str(doc.metadata.get("page", "Unknown")),
                    "text": doc.page_content[:150] + "..." if len(doc.page_content) > 150 else doc.page_content
                }
                sources.append(source)
            
            return {
                "answer": result["answer"],
                "sources": sources
            }
        except Exception as e:
            print(f"Error processing chat: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error processing chat: {str(e)}")
    except Exception as e:
        print(f"Outer error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)