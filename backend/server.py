from fastapi import FastAPI, UploadFile, File
import uvicorn
from pathlib import Path

app = FastAPI(title="MAN AI Checker API")

# Папка для збереження фото на Mac
UPLOAD_DIR = Path(__file__).resolve().parent / "uploaded_images"
UPLOAD_DIR.mkdir(exist_ok=True)

@app.post("/upload/")
async def upload_image(file: UploadFile = File(...)):
    contents = await file.read()
    
    # Сервер автоматично бере назву файлу від клієнта (тепер це буде .png)
    save_path = UPLOAD_DIR / file.filename
    with open(save_path, "wb") as f:
        f.write(contents)
        
    print(f"\n[СЕРВЕР] Збережено фото: {save_path} (розмір: {len(contents)} байт)")
    
    return {
        "status": "success",
        "message": "Фото успішно отримано та збережено на Mac",
        "file_path": str(save_path)
    }

if __name__ == "__main__":
    print("Сервер запущено. Очікую на PNG фотографії...")
    uvicorn.run(app, host="0.0.0.0", port=8000)