# Blockchain Evidence Integration

Phần Blockchain chỉ lưu `violation_id` và `evidence_hash`. Ảnh/video vẫn nằm ở backend/database, không đưa media lên blockchain.

## 1. Cài Node.js

Cài Node.js LTS nếu máy chưa có: https://nodejs.org/

## 2. Cài dependency blockchain

```powershell
cd D:\BlockChain\blockchan\blockchain
npm install
```

## 3. Cấu hình SepoliaETH

Tạo RPC Sepolia từ Infura, Alchemy, QuickNode hoặc nhà cung cấp RPC bạn dùng. Ví deploy cần có SepoliaETH testnet để trả gas.

```powershell
cd D:\BlockChain\blockchan\blockchain
$env:SEPOLIA_RPC_URL = "https://..."
$env:SEPOLIA_PRIVATE_KEY = "private_key_vi_test_khong_co_0x"
```

Không đưa private key thật vào frontend và không commit private key lên git.

## 4. Deploy contract lên Sepolia

```powershell
cd D:\BlockChain\blockchan\blockchain
npx hardhat run scripts/deploy.js --network sepolia
```

Sau khi deploy, script sẽ in contract address và tạo:

- `blockchain/deployments/contract-address.json`
- `blockchain/deployments/SpeedViolationEvidence.json`

Frontend sẽ đọc 2 file này qua Flask. Nếu file deployment còn `chainId: 31337`, đó là contract local cũ và không dùng được trên Sepolia.

## 5. Kết nối MetaMask với Sepolia

Trong MetaMask, bật Sepolia test network hoặc thêm network:

- Network name: `Sepolia`
- RPC URL: RPC Sepolia của bạn
- Chain ID: `11155111`
- Currency symbol: `SepoliaETH`

Nạp SepoliaETH testnet từ faucet vào ví trước khi ghi giao dịch.

## 6. Chạy backend hiện có

Từ thư mục gốc project:

```powershell
cd D:\BlockChain\blockchan
python web_app.py
```

## 7. Chạy frontend hiện có

Mở trình duyệt vào:

```text
http://127.0.0.1:5050/web/index.html
```

## 8. Chạy hệ thống AI như cũ

Nạp video/camera, chọn vùng xử lý, cấu hình speed limit và bấm `Chạy hệ thống`.

## 9. Ghi và xác thực Blockchain

Trên section `Minh chứng Blockchain`:

1. Bấm `Sepolia 11155111`.
2. Bấm `Kết nối MetaMask`.
3. Kiểm tra contract address tự nạp hoặc paste contract address đã deploy lên Sepolia.
4. Chọn một vi phạm.
5. Bấm `Ghi Hash lên Blockchain`.
6. Xác nhận giao dịch bằng SepoliaETH trong MetaMask.
7. Backend tự lưu `blockchain_tx_hash` và chuyển `blockchain_status = CONFIRMED`.
8. Bấm `Xác thực Blockchain`.

Thông báo hợp lệ:

```text
Minh chứng hợp lệ, HashCode khớp với Blockchain.
```

Nếu dữ liệu local bị sửa làm hash khác với hash trên contract, hệ thống sẽ báo không khớp.

## API mới

- `GET /api/violations`
- `GET /api/violations/{violation_id}`
- `POST /api/violations/{violation_id}/generate-hash`
- `POST /api/violations/{violation_id}/verify-local`
- `PATCH /api/violations/{violation_id}/blockchain`
