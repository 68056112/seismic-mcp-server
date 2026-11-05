# Seismic MCP Server (Streamable HTTP + Docker)

MCP server สำหรับข้อมูลแผ่นดินไหว (USGS และกรมอุตุนิยมวิทยา หรือ TMD) และการวิเคราะห์ประกันภัยแบบ parametric
พัฒนาตาม `spec.md` แต่ใช้ transport แบบ **Streamable HTTP** เป็นค่าเริ่มต้น และรันใน Docker container ได้
ยังใช้โหมด stdio ได้เหมือนเดิม

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `seismic_core.py` | เรียก API, แปลงข้อมูลเป็น `EarthquakeEvent`, ตรรกะ analytics และ schema ของ tools |
| `seismic_mcp_server.py` | เชื่อมกับ MCP (`initialize`, `tools/list`, `tools/call`) และ transport แบบ HTTP และ stdio |
| `Dockerfile`, `docker-compose.yml` | build และรัน container |
| `test_client.py` | ทดสอบแบบ end-to-end ด้วย MCP Python client อย่างเป็นทางการ |

## รันด้วย Docker

```bash
docker compose up -d --build
```

- MCP endpoint: `http://localhost:8000/mcp`
- Health check: `http://localhost:8000/health`

ถ้าไม่ใช้ compose:

```bash
docker build -t seismic-mcp-server:0.1.0 .
docker run -d -p 8000:8000 --name seismic-mcp seismic-mcp-server:0.1.0
```

ทดสอบ:

```bash
pip install mcp==2.3.0 && python test_client.py http://localhost:8000/mcp
```

หรือทดสอบด้วย curl:

```bash
curl -s http://localhost:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## เชื่อมต่อกับ client

**Claude Code**
```bash
claude mcp add --transport http seismic-mcp http://localhost:8000/mcp
```

**Claude Desktop** (HTTP ผ่านตัวเชื่อม `mcp-remote`)
```jsonc
{
  "mcpServers": {
    "seismic-mcp": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"] }
  }
}
```

**Claude Desktop** (stdio ภายใน Docker)
```jsonc
{
  "mcpServers": {
    "seismic-mcp": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "seismic-mcp-server:0.1.0", "python", "seismic_mcp_server.py", "--transport", "stdio"]
    }
  }
}
```

**MCP Inspector**: รัน `npx @modelcontextprotocol/inspector` แล้วเลือก transport เป็น "Streamable HTTP" และใส่ URL `http://localhost:8000/mcp`

## การตั้งค่า (environment variables)

| ตัวแปร | ค่าเริ่มต้น | คำอธิบาย |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` หรือ `stdio` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | |
| `MCP_STATELESS` | `true` | โหมด stateless ไม่เก็บ `Mcp-Session-Id` จึง restart หรือ scale container ได้อิสระ |
| `MCP_JSON_RESPONSE` | `true` | `true` ตอบเป็น `application/json` ส่วน `false` ตอบเป็น SSE stream |
| `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS` | (ว่าง) | รายการคั่นด้วย comma ถ้ากำหนดจะเปิดการป้องกัน DNS rebinding |
| `SEISMIC_TZ` | `Asia/Bangkok` | timezone ที่ใช้คำนวณ "วันนี้" ของ tools TMD |
| `SEISMIC_HTTP_TIMEOUT` | `60` | timeout (วินาที) ในการเรียก API ภายนอก |
| `SEISMIC_TMD_CACHE_TTL` | `120` | จำนวนวินาทีที่ cache ผลจาก TMD |
| `LOG_LEVEL` | `info` | log จะออกที่ stderr |

## Tools

| Tool | คำอธิบาย |
|---|---|
| `usgs_search_earthquakes` | ค้นหาเหตุการณ์จาก USGS FDSN (ช่วงเวลา, ขนาด, รัศมี) ถ้าค้นตามรัศมีต้องใส่ `latitude`, `longitude` และ `maxradiuskm` ครบทั้งสามค่า |
| `tmd_get_daily_earthquakes` | เหตุการณ์จาก TMD ของวันที่กำหนด (`date`; ถ้าไม่ใส่จะใช้วันนี้ตามเวลาไทย) |
| `tmd_get_recent_earthquakes` | เหตุการณ์จาก TMD ย้อนหลัง N วัน (1–7) ตัดรายการซ้ำและเรียงจากใหม่ไปเก่า |
| `match_events_to_assets` | จับคู่เหตุการณ์กับทรัพย์สินที่เอาประกันตามระยะทาง (haversine) และขนาดขั้นต่ำ |
| `compute_parametric_payout` | คำนวณการจ่ายสินไหมตาม payout curve แบ่งตามระยะทาง (ค่าเริ่มต้น 50 km → 50%, 100 km → 25%, 200 km → 10%) |

error ใช้รูปแบบ JSON-RPC: `-32602` เมื่อ argument ไม่ถูกต้องหรือไม่มี tool นั้น, `-32001` เมื่อ API ภายนอกล้มเหลว และ `-32002` เมื่ออ่านผลจาก API ภายนอกไม่ได้

## จุดที่ต่างจาก spec (จากการทดสอบ API จริงเมื่อ 2026-11-05)

1. **API จริงของ TMD มีรูปแบบต่างจาก spec:** รายการ event อยู่ใน `DailyEarthquakes` (ไม่ใช่ `DailySeismicEvent`) ฟิลด์เป็น `DateTimeUTC`, `DateTimeThai`, `Depth`, `OriginThai` และตัวเลขเป็น string และไม่มี `EventID` server รองรับทั้งสองรูปแบบ:
   - `id` = `TMD-` + `EventID` ถ้ามี ถ้าไม่มีจะใช้ `TMD-` + hash ของ (เวลา, lat, lon, ขนาด) ซึ่งได้ค่าเดิมทุกครั้งที่เรียก
   - `time` = `DateTime` หรือแปลง `DateTimeUTC` เป็น ISO8601 แบบ `Z`
   - `place` = `Location` หรือ `OriginThai`
2. **TMD ไม่สนพารามิเตอร์ `Date`:** API คืนข้อมูลตั้งแต่ต้นปีเสมอ (ราว 1,300 รายการขึ้นไป) server ยังส่ง `Date` ไปตาม spec แล้วกรองตามวันที่เวลาไทย (`DateTimeThai`) เอง `tmd_get_recent_earthquakes` จึงยิง request ครั้งเดียวแทน N ครั้ง (ได้ผลเหมือนกันแต่เร็วกว่ามาก) และ cache ผลไว้ 120 วินาที
3. **capabilities ใน `initialize`** ใช้รูปแบบมาตรฐานของ MCP (`{"tools": {"listChanged": false}}`) ที่ SDK สร้างให้ แทน `tools.list/call = true` ตาม spec เพราะ client มาตรฐานคาดหวังรูปแบบนี้
4. ผลลัพธ์ส่งกลับทั้งใน `structuredContent` (JSON object) และ `content[0].text` (JSON เดียวกันในรูป string) เพื่อให้ client ทุกตัวอ่านได้
5. `payout_curve` จะถูกเรียงตาม `max_distance_km` ก่อนเลือก band แรกที่ตรงเงื่อนไข จึงไม่จำเป็นต้องใส่ band ตามลำดับ
