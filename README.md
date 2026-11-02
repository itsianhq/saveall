# SaveAll

Paste a video link → pick Video/Audio + format → Download.
Works with YouTube, TikTok (no watermark), Instagram, X, Reddit, Facebook, Vimeo, and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Run

```sh
docker compose up -d --build
```

Open http://localhost:8000

Files download through your browser. To also keep a copy of every download in `./downloads`,
set `KEEP_FILES=true` in `docker-compose.yml` and run `docker compose up -d`.

## Stop / update

```sh
docker compose down          # stop
docker compose restart       # restart (also updates yt-dlp to latest)
```
