module.exports = {
  apps: [{
    name: 'man-backend',
    cwd: '/home/noname/backend',
    script: '/home/noname/.venv/bin/python',
    args: '-m uvicorn server:app --host 0.0.0.0 --port 6090',
    interpreter: 'none',
    autorestart: true,
    watch: false,
    max_restarts: 20,
    restart_delay: 2000,
    env: {
      PYTHONUNBUFFERED: '1',
      INDRA_OCR_THREADS: '10',
      OCR_BACKEND: 'claude',
      CLAUDE_OCR_MODEL: 'claude-sonnet-5-5',
      CLAUDE_DETECT_MODEL: 'claude-sonnet-5-5',
      DETECT_BACKEND: 'claude',
      QWEN_WEB_SEARCH: '1'
    }
  }]
};
