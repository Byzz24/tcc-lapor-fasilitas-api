from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import pymysql
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import uuid
from datetime import datetime
import jwt
import bcrypt
from google.cloud import storage
import secrets
import firebase_admin
from firebase_admin import credentials, messaging

# Inisialisasi Firebase Admin
try:
    # untuk testing lokal
    if os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    else:
        # 2. Jika file tidak ada (saat di-deploy ke Cloud Run), gunakan kredensial bawaan GCP
        firebase_admin.initialize_app()
except ValueError:
    # Mengabaikan error jika aplikasi sudah terinisialisasi sebelumnya
    pass

# Fungsi pembantu untuk menembakkan notifikasi ke HP
def kirim_notifikasi(fcm_token: str, judul: str, pesan: str):
    if not fcm_token: return
    try:
        pesan_fcm = messaging.Message(
            notification=messaging.Notification(title=judul, body=pesan),
            token=fcm_token,
        )
        messaging.send(pesan_fcm)
        print(f"Notifikasi berhasil dikirim ke: {fcm_token[:10]}...")
    except Exception as e:
        print(f"Gagal mengirim notifikasi: {str(e)}")

# Memuat variabel dari file .env
load_dotenv()

# Inisialisasi Aplikasi FastAPI
app = FastAPI(
    title="API Lapor Fasilitas Umum",
    description="Backend API Sistem Pelaporan Kerusakan Fasilitas Umum TCC",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# KONFIGURASI DATABASE
# ==========================================
# 1. Koneksi MySQL
def get_mysql_connection():
    instance_connection_name = os.getenv("INSTANCE_CONNECTION_NAME")
    
    if instance_connection_name:
        # Berjalan di Cloud Run (Menggunakan Unix Socket)
        return pymysql.connect(
            unix_socket=f"/cloudsql/{instance_connection_name}",
            user=os.getenv("MYSQL_USER", "admin_api"),
            password=os.getenv("MYSQL_PASSWORD", "projectTCC123!"),
            database=os.getenv("MYSQL_DB", "db_lapor_fasilitas"),
            cursorclass=pymysql.cursors.DictCursor
        )
    else:
        # Berjalan di Laptop (Menggunakan TCP/IP)
        return pymysql.connect(
            host=os.getenv("MYSQL_HOST", "localhost"),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DB", "db_lapor_fasilitas"),
            cursorclass=pymysql.cursors.DictCursor
        )

# 2. Koneksi MongoDB Atlas
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db_nosql = mongo_client["db_lapor_nosql"]
koleksi_laporan = db_nosql["detail_laporan_lapangan"]


# ==========================================
# MODEL DATA (Untuk Dokumentasi Swagger UI)
# ==========================================
class LaporanMasuk(BaseModel):
    pelapor_id: int
    kategori_id: int
    lokasi_administratif: str
    deskripsi_kerusakan: str
    latitude: float
    longitude: float
    akurasi_meter: float
    url_foto_bukti: List[str] = []

# ==========================================
# ENDPOINT API
# ==========================================

# Endpoint 1: Health Check Database
@app.get("/api/health")
def health_check():
    status_mysql = "Disconnected"
    status_mongo = "Disconnected"
    
    # Cek MySQL
    try:
        conn = get_mysql_connection()
        conn.close()
        status_mysql = "Connected"
    except Exception as e:
        status_mysql = str(e)
        
    # Cek MongoDB
    try:
        mongo_client.admin.command('ping')
        status_mongo = "Connected"
    except Exception as e:
        status_mongo = str(e)

    return {
        "status": "API Berjalan",
        "database": {
            "mysql": status_mysql,
            "mongodb": status_mongo
        }
    }

# Endpoint 2: Create Laporan (Menulis ke SQL dan NoSQL secara bersamaan)
@app.post("/api/laporan")
def buat_laporan_baru(data: LaporanMasuk):
    # Buat UUID sebagai The Bridge
    laporan_id = str(uuid.uuid4())
    waktu_sekarang = datetime.now()

    # 1. Simpan ke MySQL (Data Terstruktur)
    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO laporan (id, pelapor_id, kategori_id, lokasi_administratif, deskripsi_kerusakan)
            VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                laporan_id, data.pelapor_id, data.kategori_id, 
                data.lokasi_administratif, data.deskripsi_kerusakan
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error MySQL: {str(e)}")

    # 2. Simpan ke MongoDB (Data Spasial & Multimedia)
    try:
        dokumen_nosql = {
            "laporan_id": laporan_id,
            "lokasi_presisi": {
                "latitude": data.latitude,
                "longitude": data.longitude,
                "akurasi_meter": data.akurasi_meter
            },
            "url_foto_bukti": data.url_foto_bukti,
            "riwayat_pembaruan": [
                {
                    "status": "menunggu_validasi",
                    "waktu": waktu_sekarang.isoformat(),
                    "diperbarui_oleh": "sistem",
                    "catatan": "Laporan awal diterima"
                }
            ],
            "komentar_publik": []
        }
        koleksi_laporan.insert_one(dokumen_nosql)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error MongoDB: {str(e)}")

    # Mengirim Notifikasi ke semua Petugas
    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT fcm_token FROM users WHERE role IN ('petugas', 'admin', 'dinas') AND fcm_token IS NOT NULL")
            petugas_tokens = cursor.fetchall()
        conn.close()

        for p in petugas_tokens:
            kirim_notifikasi(
                p['fcm_token'], 
                "Laporan Baru Masuk!", 
                f"Ada kerusakan baru di area {data.lokasi_administratif}. Segera cek aplikasi!"
            )
    except Exception as e:
        print(f"Sistem notifikasi petugas error: {e}")
    return {"pesan": "Laporan berhasil dibuat", "laporan_id": laporan_id}

# Endpoint 3: Read Detail Laporan (Menggabungkan data SQL dan NoSQL)
@app.get("/api/laporan/{laporan_id}")
def dapatkan_detail_laporan(laporan_id: str):
    # 1. Ambil data dasar dari MySQL
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM laporan WHERE id = %s", (laporan_id,))
        data_sql = cursor.fetchone()
    conn.close()

    if not data_sql:
        raise HTTPException(status_code=404, detail="Laporan tidak ditemukan di MySQL")

    # 2. Ambil data tambahan dari MongoDB
    data_nosql = koleksi_laporan.find_one({"laporan_id": laporan_id}, {"_id": 0})

    if not data_nosql:
        raise HTTPException(status_code=404, detail="Detail laporan tidak ditemukan di MongoDB")

    # Gabungkan (Merge) kedua data sebagai respon API utuh
    return {
        "informasi_umum": data_sql,
        "detail_lapangan": data_nosql
    }

# ==========================================
# MODEL DATA Pydantic
# ==========================================
class UserBaru(BaseModel):
    nama: str
    email: str
    password_hash: str
    role: str = "warga"
    no_telp: str

class KategoriBaru(BaseModel):
    nama_kategori: str
    deskripsi_standar: str

class StatusUpdate(BaseModel):
    status_baru: str
    diperbarui_oleh: str
    catatan: str

class KomentarBaru(BaseModel):
    user_id: int
    nama_samaran: str
    teks_komentar: str

class PenugasanBaru(BaseModel):
    petugas_id: int
    catatan_dinas: str

class UserLogin(BaseModel):
    email: str
    password: str

class LupaPasswordRequest(BaseModel):
    email: str

class UpdatePasswordRequest(BaseModel):
    password_lama: str
    password_baru: str

# --- MODEL BARU UNTUK PROFIL & NOTIFIKASI ---
class UpdateProfilRequest(BaseModel):
    nama: str
    no_telp: str

class FCMTokenRequest(BaseModel):
    fcm_token: str

# --- ENTITAS USERS ---
# Endpoint 4: Get All Users
@app.get("/api/users")
def get_semua_users():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, nama, email, role FROM users")
        users = cursor.fetchall()
    conn.close()
    return {"data": users}

# Endpoint 5: Create User (Register)
@app.post("/api/users")
def register_user(user: UserBaru):
    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT INTO users (nama, email, password_hash, role, no_telp) VALUES (%s, %s, %s, %s, %s)"
            cursor.execute(sql, (user.nama, user.email, user.password_hash, user.role, user.no_telp))
        conn.commit()
    finally:
        conn.close()
    return {"pesan": "User berhasil didaftarkan"}

# Endpoint 6: Delete User
@app.delete("/api/users/{user_id}")
def hapus_user(user_id: int):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    return {"pesan": f"User {user_id} berhasil dihapus"}

# --- ENTITAS KATEGORI ---
# Endpoint 7: Get All Kategori
@app.get("/api/kategori")
def get_kategori():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM kategori_kerusakan")
        kategori = cursor.fetchall()
    conn.close()
    return {"data": kategori}

# Endpoint 8: Create Kategori
@app.post("/api/kategori")
def tambah_kategori(kat: KategoriBaru):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = "INSERT INTO kategori_kerusakan (nama_kategori, deskripsi_standar) VALUES (%s, %s)"
        cursor.execute(sql, (kat.nama_kategori, kat.deskripsi_standar))
    conn.commit()
    conn.close()
    return {"pesan": "Kategori ditambahkan"}

# --- ENTITAS LAPORAN (LANJUTAN) ---
# Endpoint 9: Get All Laporan (Daftar singkat untuk Admin)
@app.get("/api/laporan")
def get_semua_laporan():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, lokasi_administratif, status_perbaikan FROM laporan")
        laporan = cursor.fetchall()
    conn.close()
    return {"data": laporan}

# Endpoint 10: Update Status Laporan (SQL & NoSQL)
@app.put("/api/laporan/{laporan_id}/status")
def update_status_laporan(laporan_id: str, update: StatusUpdate):
    # Update MySQL
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("UPDATE laporan SET status_perbaikan = %s WHERE id = %s", (update.status_baru, laporan_id))
        
        # Ambil pelapor_id untuk keperluan notifikasi
        cursor.execute("SELECT pelapor_id FROM laporan WHERE id = %s", (laporan_id,))
        hasil = cursor.fetchone()
        pelapor_id = hasil['pelapor_id'] if hasil else None
        
    conn.commit()
    conn.close()

    # Update History di MongoDB
    waktu_sekarang = datetime.now().isoformat()
    koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {"$push": {"riwayat_pembaruan": {
            "status": update.status_baru,
            "waktu": waktu_sekarang,
            "diperbarui_oleh": update.diperbarui_oleh,
            "catatan": update.catatan
        }}}
    )
    
    # Mengirim Notifikasi ke HP Pelapor (Warga)
    if pelapor_id:
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute("SELECT fcm_token FROM users WHERE id = %s", (pelapor_id,))
                user = cursor.fetchone()
            conn.close()

            if user and user.get('fcm_token'):
                status_terbaca = update.status_baru.replace('_', ' ').title()
                kirim_notifikasi(
                    user['fcm_token'], 
                    "Pembaruan Status Laporan", 
                    f"Laporan Anda sekarang berstatus: {status_terbaca}"
                )
        except Exception as e:
            print(f"Sistem notifikasi warga error: {e}")
    return {"pesan": "Status pelaporan berhasil diperbarui"}

# Endpoint 11: Delete Laporan (Hapus dari SQL & NoSQL)
@app.delete("/api/laporan/{laporan_id}")
def hapus_laporan(laporan_id: str):
    # Hapus dari MySQL
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM laporan WHERE id = %s", (laporan_id,))
    conn.commit()
    conn.close()

    # Hapus dari MongoDB
    koleksi_laporan.delete_one({"laporan_id": laporan_id})
    return {"pesan": "Laporan beserta data spasialnya berhasil dihapus"}

# Endpoint 12: Tambah Komentar Warga (Hanya NoSQL)
@app.post("/api/laporan/{laporan_id}/komentar")
def tambah_komentar(laporan_id: str, komen: KomentarBaru):
    waktu_sekarang = datetime.now().isoformat()
    koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {"$push": {"komentar_publik": {
            "user_id": komen.user_id,
            "nama_samaran": komen.nama_samaran,
            "teks_komentar": komen.teks_komentar,
            "waktu": waktu_sekarang
        }}}
    )
    return {"pesan": "Komentar berhasil ditambahkan"}

# --- ENTITAS PENUGASAN PETUGAS ---
# Endpoint 13: Tugaskan Petugas
@app.post("/api/laporan/{laporan_id}/penugasan")
def tugaskan_petugas(laporan_id: str, tugas: PenugasanBaru):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = "INSERT INTO penugasan_petugas (laporan_id, petugas_id, catatan_dinas) VALUES (%s, %s, %s)"
        cursor.execute(sql, (laporan_id, tugas.petugas_id, tugas.catatan_dinas))
    conn.commit()
    conn.close()
    return {"pesan": "Petugas berhasil ditugaskan"}

# Endpoint 14: Lihat Petugas Bertugas
@app.get("/api/laporan/{laporan_id}/penugasan")
def get_penugasan(laporan_id: str):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM penugasan_petugas WHERE laporan_id = %s", (laporan_id,))
        tugas = cursor.fetchall()
    conn.close()
    return {"data": tugas}

# --- ENTITAS REKAP DINAS ---
# Endpoint 15: Ambil Rekap Bulanan
@app.get("/api/rekap/{bulan_tahun}")
def get_rekap_bulanan(bulan_tahun: str):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM rekap_dinas WHERE bulan_tahun = %s", (bulan_tahun,))
        rekap = cursor.fetchone()
    conn.close()
    if not rekap:
        return {"pesan": f"Belum ada data rekap untuk bulan {bulan_tahun}"}
    return {"data": rekap}

# Konfigurasi JWT 
JWT_SECRET = os.getenv("JWT_SECRET", "KunciRahasiaTCC2026")
JWT_ALGORITHM = "HS256"

# --- ENTITAS AUTENTIKASI & KEAMANAN ---

# Endpoint 16: Login User (Memverifikasi Password & Mengembalikan Token JWT)
@app.post("/api/auth/login")
def login_user(kredensial: UserLogin):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        # Cari user berdasarkan email
        cursor.execute("SELECT * FROM users WHERE email = %s", (kredensial.email,))
        user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Email atau password salah")

    # Verifikasi password yang di-input dengan hash di database
    try:
        password_cocok = bcrypt.checkpw(
            kredensial.password.encode('utf-8'), 
            user['password_hash'].encode('utf-8')
        )
    except Exception:
        # Fallback jika password di DB masih berupa teks biasa 
        password_cocok = (kredensial.password == user['password_hash'])

    if not password_cocok:
        raise HTTPException(status_code=401, detail="Email atau password salah")

    # Membuat token JWT yang berisi ID, Nama, dan Role User
    payload = {
        "user_id": user["id"],
        "nama": user["nama"],
        "role": user["role"]
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    # UBAH BAGIAN RETURN INI SAJA:
    return {
        "pesan": "Login berhasil",
        "access_token": token,
        "token_type": "bearer",
        "user_info": {
            "id": user["id"],
            "nama": user["nama"],
            "email": user["email"],               # TAMBAHKAN BARIS INI
            "role": user["role"],
            "no_telp": user.get("no_telp", "")    # TAMBAHKAN BARIS INI (Gunakan .get agar aman jika NULL)
        }
    }

# Endpoint 17: Refresh Token Sesi
@app.post("/api/auth/refresh-token")
def refresh_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token tidak valid atau tidak disertakan")
    
    token_lama = authorization.split(" ")[1]
    try:
        # Dekode token lama untuk mengambil payload data
        payload = jwt.decode(token_lama, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        # Buat token baru dengan data yang sama untuk memperpanjang masa aktif
        token_baru = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        return {"access_token": token_baru, "token_type": "bearer"}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token sudah kedaluwarsa, silakan login ulang")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token rusak atau tidak dikenali")

# Endpoint 18: Lupa Password (Simulasi Pengiriman Kode OTP/Link)
@app.post("/api/auth/lupa-password")
def lupa_password(request: LupaPasswordRequest):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM users WHERE email = %s", (request.email,))
        user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="Email tidak terdaftar dalam sistem")
    
    # Bagian ini akan memicu fungsi SMTP Email Server (seperti SendGrid/Mailgun)
    return {
        "pesan": f"Instruksi pemulihan kata sandi telah dikirimkan ke {request.email}. Silakan periksa kotak masuk Anda."
    }

# Endpoint 19: Ganti Password Profil
@app.put("/api/users/{user_id}/profile/password")
def ganti_password_profil(user_id: int, data: UpdatePasswordRequest):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        # Ambil hash password saat ini
        cursor.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            conn.close()
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
            
        # Validasi password lama
        try:
            password_lama_cocok = bcrypt.checkpw(data.password_lama.encode('utf-8'), user['password_hash'].encode('utf-8'))
        except Exception:
            password_lama_cocok = (data.password_lama == user['password_hash'])

        if not password_lama_cocok:
            conn.close()
            raise HTTPException(status_code=400, detail="Password lama yang Anda masukkan salah")

        # Hash password baru sebelum disimpan ke database
        salt = bcrypt.gensalt()
        hashed_baru = bcrypt.hashpw(data.password_baru.encode('utf-8'), salt).decode('utf-8')

        # Update ke database
        cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hashed_baru, user_id))
        conn.commit()
    conn.close()
    
    return {"pesan": "Kata sandi profil berhasil diperbarui"}

# ==========================================
# FITUR LANJUTAN LAPORAN (MOBILE-FRIENDLY)
# ==========================================

# PERBAIKAN: Mengubah pola URL (/api/feed/laporan) agar tidak bentrok dengan rute detail (/api/laporan/{laporan_id})
# Endpoint 20: Feed Laporan (Pagination / Infinite Scroll)
@app.get("/api/feed/laporan")
def get_laporan_feed(limit: int = 10, offset: int = 0):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = f"""
        SELECT id, lokasi_administratif, deskripsi_kerusakan, status_perbaikan 
        FROM laporan 
        LIMIT {int(limit)} OFFSET {int(offset)}
        """
        cursor.execute(sql)
        laporan = cursor.fetchall()
    conn.close()
    return {
        "pesan": "Berhasil memuat feed",
        "data": laporan, 
        "limit": limit, 
        "offset": offset
    }

# Endpoint 21: Pencarian Laporan (Search)
@app.get("/api/search/laporan")
def cari_laporan(keyword: str):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = """
        SELECT id, lokasi_administratif, deskripsi_kerusakan, status_perbaikan 
        FROM laporan 
        WHERE lokasi_administratif LIKE %s OR deskripsi_kerusakan LIKE %s
        """
        wildcard_keyword = f"%{keyword}%"
        cursor.execute(sql, (wildcard_keyword, wildcard_keyword))
        hasil = cursor.fetchall()
    conn.close()
    return {"data": hasil, "keyword": keyword}

# Endpoint 22: Filter Laporan (Berdasarkan Kategori & Status)
@app.get("/api/filter/laporan")
def filter_laporan(kategori_id: int = None, status: str = None):
    conn = get_mysql_connection()
    query = "SELECT id, lokasi_administratif, deskripsi_kerusakan, status_perbaikan FROM laporan WHERE 1=1"
    params = []
    
    if kategori_id is not None:
        query += " AND kategori_id = %s"
        params.append(kategori_id)
    if status is not None:
        query += " AND status_perbaikan = %s"
        params.append(status)
        
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        hasil = cursor.fetchall()
    conn.close()
    return {"data": hasil}

# Endpoint 23: Fitur Upvote / Dukungan Warga (Disimpan di NoSQL)
@app.post("/api/laporan/{laporan_id}/upvote")
def upvote_laporan(laporan_id: str, user_id: int):
    hasil = koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {"$addToSet": {"dukungan_warga": user_id}}
    )
    
    if hasil.matched_count == 0:
        raise HTTPException(status_code=404, detail="Laporan tidak ditemukan di sistem spasial")
        
    if hasil.modified_count == 0:
        return {"pesan": "Anda sudah memberikan dukungan untuk laporan ini sebelumnya."}
        
    return {"pesan": "Dukungan (upvote) berhasil ditambahkan"}

# Konfigurasi Google Cloud Storage Bucket
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "tcc-bucket-lapor")

def upload_ke_gcs(file: UploadFile, folder_tujuan: str) -> str:
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        
        ekstensi = file.filename.split(".")[-1]
        nama_file_unik = f"{folder_tujuan}/{secrets.token_hex(16)}.{ekstensi}"
        
        blob = bucket.blob(nama_file_unik)
        blob.upload_from_file(file.file, content_type=file.content_type)
        
        return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{nama_file_unik}"
    except Exception as e:
        print(f"GCS Upload Log: {str(e)}")
        ekstensi = file.filename.split(".")[-1]
        return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{folder_tujuan}/mock_{secrets.token_hex(4)}.{ekstensi}"

# --- ENTITAS MANAJEMEN MULTIMEDIA ---
# Endpoint 24: Upload Foto Bukti Kerusakan Fasilitas
@app.post("/api/upload/foto-kerusakan")
def upload_foto_kerusakan(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File harus berupa gambar (JPEG/PNG)")
        
    url_publik = upload_ke_gcs(file, folder_tujuan="foto_laporan")
    return {
        "pesan": "Foto bukti kerusakan berhasil diunggah",
        "url_foto": url_publik
    }

# Endpoint 25: Upload Foto Profil / Avatar User
@app.post("/api/upload/avatar-user")
def upload_avatar_user(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File harus berupa gambar (JPEG/PNG)")
        
    url_publik = upload_ke_gcs(file, folder_tujuan="avatar_user")
    return {
        "pesan": "Foto profil berhasil diperbarui",
        "url_foto": url_publik
    }

# ==========================================
# MODEL DATA Aktivitas Petugas
# ==========================================
class KonfirmasiSelesai(BaseModel):
    url_foto_perbaikan: str
    catatan_petugas: str

# --- ENTITAS AKTIVITAS PETUGAS LAPANGAN---
# Endpoint 26: Petugas Menerima Tugas & Menuju Lokasi
@app.put("/api/penugasan/{tugas_id}/terima")
def petugas_terima_tugas(tugas_id: int):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT laporan_id FROM penugasan_petugas WHERE id = %s", (tugas_id,))
        tugas = cursor.fetchone()
        if not tugas:
            conn.close()
            raise HTTPException(status_code=404, detail="ID Penugasan tidak ditemukan di MySQL")
        
        laporan_id = tugas['laporan_id']
        cursor.execute("UPDATE laporan SET status_perbaikan = 'petugas_menuju_lokasi' WHERE id = %s", (laporan_id,))
    conn.commit()
    conn.close()

    waktu_sekarang = datetime.now().isoformat()
    koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {"$push": {"riwayat_pembaruan": {
            "status": "petugas_menuju_lokasi",
            "waktu": waktu_sekarang,
            "diperbarui_oleh": f"petugas_tugas_id_{tugas_id}",
            "catatan": "Petugas telah mengonfirmasi penugasan and sedang bergerak menuju lokasi."
        }}}
    )
    return {"pesan": "Konfirmasi penugasan berhasil, petugas sedang menuju lokasi."}

# Endpoint 27: Petugas Mulai Mengeksekusi Perbaikan Fisik (In Progress)
@app.put("/api/penugasan/{tugas_id}/progress")
def petugas_mulai_perbaikan(tugas_id: int):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT laporan_id FROM penugasan_petugas WHERE id = %s", (tugas_id,))
        tugas = cursor.fetchone()
        if not tugas:
            conn.close()
            raise HTTPException(status_code=404, detail="ID Penugasan tidak ditemukan")
        
        laporan_id = tugas['laporan_id']
        cursor.execute("UPDATE laporan SET status_perbaikan = 'sedang_diperbaiki' WHERE id = %s", (laporan_id,))
    conn.commit()
    conn.close()

    waktu_sekarang = datetime.now().isoformat()
    koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {"$push": {"riwayat_pembaruan": {
            "status": "sedang_diperbaiki",
            "waktu": waktu_sekarang,
            "diperbarui_oleh": f"petugas_tugas_id_{tugas_id}",
            "catatan": "Fasilitas rusak sedang dalam proses perbaikan teknis di lapangan."
        }}}
    )
    return {"pesan": f"Status laporan {laporan_id} berhasil diubah menjadi Sedang Diperbaiki."}

# Endpoint 28: Petugas Menyelesaikan Tugas (Wajib Mengirimkan Foto Bukti Perbaikan)
@app.post("/api/penugasan/{tugas_id}/selesai")
def petugas_selesai_perbaikan(tugas_id: int, data: KonfirmasiSelesai):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT laporan_id FROM penugasan_petugas WHERE id = %s", (tugas_id,))
        tugas = cursor.fetchone()
        if not tugas:
            conn.close()
            raise HTTPException(status_code=404, detail="ID Penugasan tidak ditemukan")
        
        laporan_id = tugas['laporan_id']
        cursor.execute("UPDATE laporan SET status_perbaikan = 'selesai' WHERE id = %s", (laporan_id,))
    conn.commit()
    conn.close()

    waktu_sekarang = datetime.now().isoformat()
    koleksi_laporan.update_one(
        {"laporan_id": laporan_id},
        {
            "$push": {
                "riwayat_pembaruan": {
                    "status": "selesai",
                    "waktu": waktu_sekarang,
                    "diperbarui_oleh": f"petugas_tugas_id_{tugas_id}",
                    "catatan": f"Perbaikan selesai. Catatan akhir petugas: {data.catatan_petugas}"
                }
            },
            "$set": {
                "foto_bukti_selesai": data.url_foto_perbaikan
            }
        }
    )
    return {"pesan": "Laporan resmi ditutup. Data bukti perbaikan fisik telah diarsipkan di SQL & NoSQL."}

# --- ENTITAS DASBOR ANALITIK ADMIN ---
# Endpoint 29: Statistik Status Laporan
@app.get("/api/analitik/statistik-status")
def get_statistik_status():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = "SELECT status_perbaikan, COUNT(*) as total FROM laporan GROUP BY status_perbaikan"
        cursor.execute(sql)
        hasil = cursor.fetchall()
    conn.close()
    return {"data": hasil}

# Endpoint 30: Kategori Kerusakan Terbanyak
@app.get("/api/analitik/kategori-terbanyak")
def get_kategori_terbanyak():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = """
        SELECT k.nama_kategori, COUNT(l.id) as total_laporan 
        FROM laporan l
        JOIN kategori_kerusakan k ON l.kategori_id = k.id
        GROUP BY k.id, k.nama_kategori
        ORDER BY total_laporan DESC
        """
        cursor.execute(sql)
        hasil = cursor.fetchall()
    conn.close()
    return {"data": hasil}

# Endpoint 31: Performa Penyelesaian Tugas oleh Petugas
@app.get("/api/analitik/performa-petugas")
def get_performa_petugas():
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        sql = """
        SELECT p.petugas_id, u.nama as nama_petugas, COUNT(p.id) as total_tugas
        FROM penugasan_petugas p
        JOIN users u ON p.petugas_id = u.id
        GROUP BY p.petugas_id, u.nama
        ORDER BY total_tugas DESC
        """
        cursor.execute(sql)
        hasil = cursor.fetchall()
    conn.close()
    return {"data": hasil}

# ==========================================
# ENDPOINT MANAJEMEN PROFIL & FCM TOKEN
# ==========================================

# Endpoint 32: Update Profil Dasar (Nama & No Telp)
@app.put("/api/users/{user_id}/profil")
def update_profil_dasar(user_id: int, data: UpdateProfilRequest):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
            
        cursor.execute("UPDATE users SET nama = %s, no_telp = %s WHERE id = %s", (data.nama, data.no_telp, user_id))
        conn.commit()
    conn.close()
    return {"pesan": "Data profil berhasil diperbarui", "data": {"nama": data.nama, "no_telp": data.no_telp}}

# Endpoint 33: Simpan FCM Token untuk Notifikasi
@app.put("/api/users/{user_id}/fcm-token")
def update_fcm_token(user_id: int, data: FCMTokenRequest):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        try:
            cursor.execute("UPDATE users SET fcm_token = %s WHERE id = %s", (data.fcm_token, user_id))
            conn.commit()
        except pymysql.err.OperationalError:
            conn.close()
            raise HTTPException(status_code=500, detail="Gagal menyimpan token. Pastikan kolom fcm_token ada di tabel users.")
    conn.close()
    return {"pesan": "Token perangkat berhasil didaftarkan untuk notifikasi"}

# Endpoint 34: Get Profil User Spesifik
@app.get("/api/users/{user_id}/profil")
def get_profil_user(user_id: int):
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT nama, email, role, no_telp, fcm_token FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
        
    # Memastikan aplikasi Flutter tidak crash jika no_telp kosong di database
    if user.get('no_telp') is None:
        user['no_telp'] = ""
        
    return {"data": user}