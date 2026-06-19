# MULA GO — Omni-Channel E-Commerce Broadcaster

MULA GO is a professional desktop application designed to streamline omni-channel product promotions. It enables online sellers to queue product images, generate SEO-optimized captions and tags, and broadcast them automatically to connected social platforms (Instagram, TikTok, and X) with one click.

The application leverages a hybrid local/cloud AI architecture to analyze product images and compile high-converting sales copy.

---

## 🌟 Key Features

- **Omni-Channel Broadcasting**: Auto-post product promotions to Instagram, TikTok, and X simultaneously.
- **Local AI Visual Grounding**: Utilizes visual detection algorithms to safely navigate login states and post-action dialogs.
- **Smart SEO Copywriter**: Automatically turns visual keywords into custom captions tailored for the unique audience and style constraints of each platform.
- **Clean Folder Workflow**: Pick folders directly from your local filesystem to queue and structure product drops.
- **Stealth Interaction**: Natural click paths and human-like typing delays to comply with platform guidelines.

---

## 🛠️ Tech Stack

- **Core Framework**: Python 3.x
- **GUI Layer**: PyWebView (HTML/JS/CSS Desktop Shell)
- **Automation Engine**: Playwright (CDP-based browser automation)
- **Local VLM & Object Detection**: Local AI inference engines for offline navigation and visual layout parsing
- **Cloud Refinement**: Gemini API REST integration for advanced copy generation

---

## 🚀 Setup & Installation

### 1. Prerequisites
Ensure you have Python 3.10+ installed on your system.

### 2. Clone the Repository & Install Dependencies
```bash
# Clone the repository
git clone https://github.com/your-username/mula-go.git
cd mula-go

# Install requirements
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 3. Environment Configuration
Create a local `.env` file in the root directory by copying the example template:
```bash
cp .env.example .env
```
Open `.env` and configure your credentials:
```env
GEMINI_API_KEY=your_actual_gemini_api_key_here
```

### 4. Running the Application
Start the desktop application using python:
```bash
python main.py
```

---

## 🔒 Security & Privacy

- **Local-First Cookies**: All social media cookies and credentials are saved locally in the `data/sessions/` directory. They are never uploaded or shared.
- **Git Safety**: Sensitive directories (like `.env` and `data/sessions/`) are excluded from Git tracking via `.gitignore` to prevent secret leaks.
