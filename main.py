from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "success",
        "message": "Backend API Lapor Fasilitas Umum Berjalan Sempurna!"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)