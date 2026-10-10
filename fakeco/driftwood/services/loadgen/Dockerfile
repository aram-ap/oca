FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.28.1
COPY loadgen.py /loadgen.py
ENTRYPOINT ["python", "-u", "/loadgen.py"]
