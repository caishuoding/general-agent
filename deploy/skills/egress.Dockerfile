FROM python:3.12-slim
COPY deploy/skills/egress.py /proxy.py
USER 65534:65534
CMD ["python", "/proxy.py"]
