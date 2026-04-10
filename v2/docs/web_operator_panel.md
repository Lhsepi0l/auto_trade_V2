# Web Operator Panel

웹 운영 콘솔은 기존 control HTTP 위에 붙는 별도 operator surface입니다.
현재 운영 기준은 web-first 이며, Discord는 선택형 fallback / emergency-use-only surface 입니다.

## 기본 실행

```bash
python -m v2.run \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --control-http \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101 \
  --operator-web
```

- 콘솔 주소: `http://127.0.0.1:8101/operator`
- 기본 access model: localhost only
- 외부 접근은 Tailscale, SSH 터널, 또는 인증된 reverse proxy 뒤에서만 허용

## 통합 스택 실행

```bash
bash v2/scripts/run_stack.sh --mode shadow --env testnet
```

- 기본값은 web-first 입니다:
  - `/operator` 활성화
  - Discord bot 비활성
- Discord fallback이 필요할 때만 아래를 사용합니다.

```bash
bash v2/scripts/run_stack.sh --mode shadow --env testnet
```

## 운영 원칙

- 정상적인 일상 운영은 웹 패널에서 수행합니다.
- Discord는 웹 패널 접근이 어렵거나 emergency 확인이 필요할 때만 켭니다.
- 문서/런북/서비스 설치 기본값은 web-first 를 기준으로 유지합니다.
