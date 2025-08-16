#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
import io
import pickle

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError

from PIL import Image
from PyPDF2 import PdfMerger
import tempfile

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

WAITING_FOR_CODE = 1

with open('config.json', 'r', encoding='utf-8') as f:
    CONFIG = json.load(f)

class GoogleDriveManager:
    def __init__(self, token_file='token.pickle'):
        with open(token_file, 'rb') as token:
            self.credentials = pickle.load(token)
        self.service = build('drive', 'v3', credentials=self.credentials)
        self.root_folder_id = None
        
    def set_root_folder(self, folder_name="SoruBankasi"):
        try:
            response = self.service.files().list(
                q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            if response.get('files'):
                self.root_folder_id = response['files'][0]['id']
                logger.info(f"Root folder found: {self.root_folder_id}")
            else:
                file_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder'
                }
                folder = self.service.files().create(
                    body=file_metadata,
                    fields='id'
                ).execute()
                self.root_folder_id = folder['id']
                logger.info(f"Root folder created: {self.root_folder_id}")
                
        except HttpError as error:
            logger.error(f"An error occurred: {error}")
            
    def create_folder_structure(self, path_parts):
        parent_id = self.root_folder_id
        
        for folder_name in path_parts:
            response = self.service.files().list(
                q=f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            if response.get('files'):
                parent_id = response['files'][0]['id']
            else:
                file_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id]
                }
                folder = self.service.files().create(
                    body=file_metadata,
                    fields='id'
                ).execute()
                parent_id = folder['id']
                
        return parent_id
        
    def upload_image(self, image_bytes, filename, folder_id):
        try:
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            
            media = MediaIoBaseUpload(
                io.BytesIO(image_bytes),
                mimetype='image/png',
                resumable=True
            )
            
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name, webViewLink'
            ).execute()
            
            return file
            
        except HttpError as error:
            logger.error(f"Upload error: {error}")
            return None
            
    def count_files_in_folder(self, folder_id):
        try:
            response = self.service.files().list(
                q=f"'{folder_id}' in parents and mimeType='image/png'",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            return len(response.get('files', []))
            
        except HttpError as error:
            logger.error(f"Count error: {error}")
            return 0
            
    def list_files_in_folder(self, folder_id):
        try:
            response = self.service.files().list(
                q=f"'{folder_id}' in parents and mimeType='image/png'",
                spaces='drive',
                orderBy='name',
                fields='files(id, name, createdTime, webViewLink)'
            ).execute()
            
            return response.get('files', [])
            
        except HttpError as error:
            logger.error(f"List error: {error}")
            return []
            
    def download_file(self, file_id):
        try:
            request = self.service.files().get_media(fileId=file_id)
            file_bytes = io.BytesIO()
            downloader = MediaIoBaseDownload(file_bytes, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
                
            file_bytes.seek(0)
            return file_bytes.read()
            
        except HttpError as error:
            logger.error(f"Download error: {error}")
            return None

class SoruBankasiBot:
    def __init__(self, token, token_pickle_file='token.pickle'):
        self.token = token
        self.drive = GoogleDriveManager(token_pickle_file)
        self.drive.set_root_folder()
        self.user_states = {}
        
    def parse_code(self, code):
        try:
            parts = code.split('.')
            if len(parts) < 3:
                return None
                
            ders_id = parts[0]
            sinav_id = parts[1]
            konu_id = parts[2]
            alt_konu_id = parts[3] if len(parts) > 3 else None
            
            if ders_id not in CONFIG['dersler']:
                return None
                
            ders = CONFIG['dersler'][ders_id]
            
            if sinav_id not in ders['sinavlar']:
                return None
                
            sinav = ders['sinavlar'][sinav_id]
            
            if konu_id not in sinav['konular']:
                return None
                
            konu = sinav['konular'][konu_id]
            
            result = {
                'ders': ders['ad'],
                'sinav': sinav['ad'],
                'konu': konu['ad'],
                'alt_konu': None,
                'code': code,
                'folder_path': [ders['ad'], sinav['ad'], konu['ad']]
            }
            
            if alt_konu_id and alt_konu_id in konu.get('alt_konular', {}):
                result['alt_konu'] = konu['alt_konular'][alt_konu_id]
                result['folder_path'].append(result['alt_konu'])
                
            return result
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None
            
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        welcome_text = f"""
🎓 **Soru Bankası Bot'a Hoş Geldiniz!**

Merhaba {user.first_name}! 

📚 **Nasıl Kullanılır:**
1. Bir soru fotoğrafı gönderin
2. Konu kodunu yazın (örn: 1.1.2.3)
3. Otomatik olarak organize edilir!

📝 **Komutlar:**
/menu - Ders ve konu listesi
/list [kod] - Konudaki soruları listele
/pdf [kodlar] - PDF oluştur
/help - Yardım

🔢 **Kod Formatı:**
Ders.Sınav.Konu.AltKonu
Örnek: 1.1.2.3 = Mat > AYT > Türev > Zincir Kuralı
"""
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
        
    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        menu_text = "📚 **DERS VE KONU YAPISI**\n\n"
        
        for ders_id, ders in CONFIG['dersler'].items():
            menu_text += f"**{ders_id}. {ders['ad']}**\n"
            
            for sinav_id, sinav in ders['sinavlar'].items():
                menu_text += f"  {ders_id}.{sinav_id} - {sinav['ad']}\n"
                
                for konu_id, konu in sinav['konular'].items():
                    menu_text += f"    {ders_id}.{sinav_id}.{konu_id} - {konu['ad']}\n"
                    
                    for alt_id, alt_konu in konu.get('alt_konular', {}).items():
                        menu_text += f"      {ders_id}.{sinav_id}.{konu_id}.{alt_id} - {alt_konu}\n"
                        
            menu_text += "\n"
            
        if len(menu_text) > 4000:
            parts = menu_text.split('\n\n')
            current_msg = ""
            
            for part in parts:
                if len(current_msg) + len(part) < 4000:
                    current_msg += part + "\n\n"
                else:
                    await update.message.reply_text(current_msg, parse_mode='Markdown')
                    current_msg = part + "\n\n"
                    
            if current_msg:
                await update.message.reply_text(current_msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(menu_text, parse_mode='Markdown')
            
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        
        photo_file = await update.message.photo[-1].get_file()
        
        photo_bytes = io.BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)
        
        self.user_states[user_id] = {
            'photo': photo_bytes.read(),
            'username': user.username or user.first_name,
            'timestamp': datetime.now()
        }
        
        await update.message.reply_text(
            "📸 Fotoğraf alındı!\n\n"
            "📝 Lütfen konu kodunu yazın.\n"
            "Örnek: 1.1.2.3"
        )
        
        return WAITING_FOR_CODE
        
    async def handle_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        code = update.message.text.strip()
        
        if user_id not in self.user_states:
            await update.message.reply_text(
                "❌ Önce bir fotoğraf göndermelisiniz!"
            )
            return ConversationHandler.END
            
        parsed = self.parse_code(code)
        if not parsed:
            await update.message.reply_text(
                "❌ Geçersiz kod!\n"
                "Lütfen geçerli bir kod girin.\n"
                "Örnek: 1.1.2.3"
            )
            return WAITING_FOR_CODE
            
        folder_id = self.drive.create_folder_structure(parsed['folder_path'])
        
        file_count = self.drive.count_files_in_folder(folder_id)
        new_number = file_count + 1
        
        timestamp = datetime.now().strftime("%H-%M")
        username = self.user_states[user_id]['username']
        filename = f"{code}.{new_number}_{username}_{timestamp}.png"
        
        photo_bytes = self.user_states[user_id]['photo']
        uploaded_file = self.drive.upload_image(photo_bytes, filename, folder_id)
        
        if uploaded_file:
            response_text = f"""
✅ **Başarıyla kaydedildi!**

📚 **Konum:**
{parsed['ders']} > {parsed['sinav']} > {parsed['konu']}"""

            if parsed['alt_konu']:
                response_text += f" > {parsed['alt_konu']}"
                
            response_text += f"""

📄 **Dosya:** {filename}
🔢 **Sıra:** {new_number}. soru
🔗 **Link:** [Google Drive'da Görüntüle]({uploaded_file['webViewLink']})
"""
            
            await update.message.reply_text(response_text, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Yükleme hatası! Lütfen tekrar deneyin.")
            
        del self.user_states[user_id]
        return ConversationHandler.END
        
    async def list_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Kullanım: /list [kod]\n"
                "Örnek: /list 1.1.2.3"
            )
            return
            
        code = context.args[0]
        parsed = self.parse_code(code)
        
        if not parsed:
            await update.message.reply_text("❌ Geçersiz kod!")
            return
            
        folder_id = self.drive.create_folder_structure(parsed['folder_path'])
        files = self.drive.list_files_in_folder(folder_id)
        
        if not files:
            await update.message.reply_text("📭 Bu konuda henüz soru yok!")
            return
            
        response = f"📚 **{parsed['konu']}**"
        if parsed['alt_konu']:
            response += f" > {parsed['alt_konu']}"
        response += f"\n\n📄 **{len(files)} soru:**\n\n"
        
        for i, file in enumerate(files, 1):
            name_parts = file['name'].split('_')
            if len(name_parts) >= 2:
                user = name_parts[-2]
                response += f"{i}. {file['name'].split('.png')[0]} - {user}\n"
            else:
                response += f"{i}. {file['name']}\n"
                
        await update.message.reply_text(response, parse_mode='Markdown')
        
    async def create_pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Kullanım: /pdf [kod1] [kod2] ...\n"
                "Örnek: /pdf 1.1.2.3.1 1.1.2.3.2"
            )
            return
            
        await update.message.reply_text("📄 PDF oluşturuluyor...")
        
        images = []
        
        for code in context.args:
            parts = code.rsplit('.', 1)
            if len(parts) == 2:
                base_code = parts[0]
                question_num = parts[1]
            else:
                base_code = code
                question_num = None
                
            parsed = self.parse_code(base_code)
            if not parsed:
                continue
                
            folder_id = self.drive.create_folder_structure(parsed['folder_path'])
            files = self.drive.list_files_in_folder(folder_id)
            
            for file in files:
                if question_num:
                    if f"{base_code}.{question_num}_" in file['name']:
                        file_bytes = self.drive.download_file(file['id'])
                        if file_bytes:
                            images.append(Image.open(io.BytesIO(file_bytes)))
                else:
                    file_bytes = self.drive.download_file(file['id'])
                    if file_bytes:
                        images.append(Image.open(io.BytesIO(file_bytes)))
                        
        if not images:
            await update.message.reply_text("❌ Görüntü bulunamadı!")
            return
            
        pdf_bytes = io.BytesIO()
        
        if images:
            images[0].save(
                pdf_bytes,
                "PDF",
                save_all=True,
                append_images=images[1:] if len(images) > 1 else []
            )
            
        pdf_bytes.seek(0)
        
        filename = f"sorular_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        await update.message.reply_document(
            document=pdf_bytes,
            filename=filename,
            caption=f"📄 {len(images)} soru içeren PDF oluşturuldu!"
        )
        
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """
📚 **YARDIM**

**Temel Kullanım:**
1. Soru fotoğrafı gönderin
2. Konu kodu yazın
3. Otomatik kaydedilir

**Kod Sistemi:**
`Ders.Sınav.Konu.AltKonu`

**Örnekler:**
• 1.1.2.3 = Mat > AYT > Türev > Zincir Kuralı
• 2.1.1.2 = Fizik > AYT > Kuvvet > Bağıl Hareket

**Komutlar:**
/start - Botu başlat
/menu - Tüm ders listesi
/list 1.1.2 - Konudaki soruları listele
/pdf 1.1.2.3.1 1.1.2.3.2 - PDF oluştur
/help - Bu mesaj

**PDF Örnekleri:**
• /pdf 1.1.2.3.1 - Tek soru
• /pdf 1.1.2.3 - Tüm alt konu
• /pdf 1.1.2 - Tüm konu
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')
        
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in self.user_states:
            del self.user_states[user_id]
        await update.message.reply_text("İşlem iptal edildi.")
        return ConversationHandler.END
        
    def run(self):
        application = Application.builder().token(self.token).build()
        
        conv_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.PHOTO, self.handle_photo)],
            states={
                WAITING_FOR_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_code)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel)]
        )
        
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("menu", self.menu))
        application.add_handler(CommandHandler("list", self.list_questions))
        application.add_handler(CommandHandler("pdf", self.create_pdf))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(conv_handler)
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN bulunamadı!")
        print("Lütfen .env dosyasına ekleyin:")
        print("TELEGRAM_TOKEN=your_bot_token_here")
        exit(1)
        
    if not os.path.exists('token.pickle'):
        print("❌ token.pickle bulunamadı!")
        print("Önce 'python auth.py' çalıştırın")
        exit(1)
        
    bot = SoruBankasiBot(TELEGRAM_TOKEN)
    print("✅ Bot başlatılıyor...")
    print("Durdurmak için Ctrl+C")
    bot.run()