# Runbook: Node.js + SQL Server + SepoliaETH + MetaMask

Tài liệu này dùng cho máy Windows hiện tại. Dự án vẫn chạy AI bằng Flask/Python, dùng Node.js/Hardhat để deploy smart contract lên Sepolia, dùng MetaMask ký giao dịch bằng SepoliaETH, và dùng SQL Server để lưu dữ liệu vi phạm.

## 1. Kiểm tra công cụ trên máy

Nếu `node`, `npm`, `sqlcmd` chưa có trong PATH, dùng đường dẫn tuyệt đối:

```powershell
& "C:\Program Files\nodejs\node.exe" --version
& "C:\Program Files\nodejs\npm.cmd" --version
& "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\SQLCMD.EXE" -?
```

## 2. Tạo database SQL Server

Chạy bằng Windows Authentication:

```powershell
cd D:\BlockChain\blockchan
& "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\SQLCMD.EXE" -S localhost -E -No -i database\sqlserver_setup.sql
```

Nếu máy báo lỗi encryption của ODBC Driver 18, kiểm tra `Force Encryption = No` cho môi trường local dev rồi restart service `SQL Server (MSSQLSERVER)`.

## 3. Cài Python dependency SQL Server

```powershell
cd D:\BlockChain\blockchan
.\.venv\Scripts\python.exe -m pip install pyodbc==5.2.0
```

## 4. Chạy backend Flask với SQL Server

```powershell
cd D:\BlockChain\blockchan
$env:VIOLATION_DB_PROVIDER = "sqlserver"
$env:SQLSERVER_CONNECTION_STRING = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=VehicleSpeedEvidence;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes"
.\.venv\Scripts\python.exe web_app.py
```

Kiểm tra API:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5050/api/violations
```

## 5. Cài dependency Node.js cho Hardhat

```powershell
cd D:\BlockChain\blockchan\blockchain
& "C:\Program Files\nodejs\npm.cmd" install
```

## 6. Cấu hình SepoliaETH

Chuẩn bị:

- `SEPOLIA_RPC_URL`: RPC Sepolia từ Infura, Alchemy, QuickNode hoặc nhà cung cấp RPC bạn dùng.
- `SEPOLIA_PRIVATE_KEY`: private key ví test dùng để deploy contract, không có tiền thật.
- Ví deploy cần có SepoliaETH từ faucet.

Thiết lập biến môi trường trong terminal deploy:

```powershell
cd D:\BlockChain\blockchan\blockchain
$env:PATH = "C:\Program Files\nodejs;" + $env:PATH
$env:SEPOLIA_RPC_URL = "https://..."
$env:SEPOLIA_PRIVATE_KEY = "private_key_vi_test_khong_co_0x"
```

Không commit private key và không đưa private key vào frontend.

## 7. Deploy smart contract lên Sepolia

```powershell
cd D:\BlockChain\blockchan\blockchain
& "C:\Program Files\nodejs\npx.cmd" hardhat run scripts/deploy.js --network sepolia
```

Copy địa chỉ ở dòng:

```text
SpeedViolationEvidence deployed to: 0x...
```

Script cũng tạo:

- `blockchain/deployments/contract-address.json`
- `blockchain/deployments/SpeedViolationEvidence.json`

## 8. Cấu hình MetaMask Sepolia

Trong MetaMask, bật Sepolia test network hoặc thêm network:

- Network name: `Sepolia`
- RPC URL: RPC Sepolia của bạn
- Chain ID: `11155111`
- Currency symbol: `SepoliaETH`

Ví cần có SepoliaETH để trả phí gas testnet.

## 9. Chạy web

Mở:

```text
http://127.0.0.1:5050/web/index.html
```

Trong section Blockchain:

1. Bấm `Sepolia 11155111`.
2. Bấm `Kết nối MetaMask`.
3. Kiểm tra contract address tự nạp hoặc paste contract address đã deploy lên Sepolia.
4. Bấm `Nạp contract`.
5. Chọn bản ghi vi phạm.
6. Bấm `Ghi Hash lên Blockchain`.
7. Xác nhận giao dịch bằng SepoliaETH trong MetaMask.
8. Bấm `Xác thực Blockchain`.

## 10. Kiểm tra dữ liệu trong SQL Server

```powershell
& "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\SQLCMD.EXE" -S localhost -E -No -d VehicleSpeedEvidence -Q "SELECT TOP 20 violation_id, speed, speed_limit, evidence_hash, blockchain_status, blockchain_tx_hash FROM dbo.speed_violations ORDER BY created_at DESC"
```

## Ghi chú vận hành

- AI không lưu ảnh/video lên blockchain.
- SQL Server lưu metadata vi phạm, `evidence_hash`, `blockchain_tx_hash`, `blockchain_status`.
- Blockchain chỉ lưu `violation_id` và `evidence_hash`.
- Nếu sửa dữ liệu vi phạm trong SQL Server, `verify-local` sẽ phát hiện hash không còn khớp.
