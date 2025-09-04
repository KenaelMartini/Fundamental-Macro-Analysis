# Central Bank Monitoring (CBM)

**Central Bank Monitoring (CBM)** is a **Python/Tkinter desktop application** designed to monitor in real time:  

- **Central banks** communications and releases (Fed, BoE, ECB, BoJ, BoC, RBA, RBNZ, SNB).  
- The **macroeconomic calendar** (TradingEconomics, with HTML fallback in case of API restrictions).  
- **Automated analysis** of publications (classification, labeling, hawkish/dovish bias).  

---

## 🎯 Purpose

CBM aims to provide a robust tool to:  
- Track monetary policy decisions in real time (G10 central banks).  
- Anticipate market moves via direct integration of the economic calendar.  
- Centralize and archive key events (JSONL, CSV, export).  
- Offer a simple and fast UI suitable for a **trading desk environment**.  

---

## ✨ Key Features

- **Dashboard**
  - Real-time heartbeats (25 ms for central banks, ~800 ms for TE).  
  - Continuous monitoring of latency and watcher status.  

- **News**
  - Live feed of macro publications (CPI, GDP, PMI, Unemployment, JOLTS, NFP, Retail Sales, etc.).  
  - Automatic categorization: Inflation, Growth, Labor Market, PMI, Retail.  
  - Complete mapping: country, currency, event, actual, consensus, previous, importance.  
  - Instant analysis (labels + potential impact).  

- **Data / History / Logs**
  - Automatic archival in JSONL.  
  - Search, filters, CSV/JSON export for external usage.  

- **UI (Tkinter)**
  - Tabs: Dashboard, News, Data, History, Logs.  
  - Controls: Start/Stop/Restart watchers, Clear, Export.  

---

## 🧑‍💻 Development

The project has been designed as a **desk-grade tool**:  
- Modular structure (independent watchers + unified interface).  
- Robustness: HTML fallback if the API is blocked, error handling, local caching.  
- Extensibility: progressive addition of new central banks and macro categories.  
- **Documentation**: almost all the code is annotated to ease readability, understanding, and further development.  

### Use of AI
During development, AI was used as:  
- A **review and verification assistant** for code.  
- A way to accelerate exploration of technical solutions (threading, HTML parsing, Tkinter UI).  
- A **learning tool** for specific technical parts.  

AI served as a **pedagogical catalyst**, but the architecture, decisions, and final implementation remain the result of personal work.  
