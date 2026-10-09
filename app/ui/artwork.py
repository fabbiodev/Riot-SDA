"""Lazy public artwork, loaded without account data or Riot session credentials."""

import json
import re
from collections import OrderedDict, deque
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QObject, QUrl, QSize, Qt, pyqtSignal, pyqtSlot, QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QImageReader
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply, QNetworkDiskCache
from PyQt6.QtWidgets import QLabel

from app.core.storage import APPDATA_DIR


def public_asset_url(value):
    url = QUrl(str(value or ""))
    return (url.scheme() == "https" and url.host() in {
        "ddragon.leagueoflegends.com", "media.valorant-api.com", "valorant-api.com"
    } and not url.userName() and not url.password() and url.port(-1) == -1
        and not url.hasQuery() and not url.hasFragment())


def league_artwork(champions, version):
    result = {}
    for alias, champion in champions.items():
        if not re.fullmatch(r"[A-Za-z0-9]+", alias):
            continue
        result[("characters", str(champion["key"]))] = (
            f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png")
        for skin in champion.get("skins", []):
            # A chroma may share its parent's artwork, but never another skin's.
            number = skin.get("parentSkin", skin.get("num"))
            if not isinstance(number, int) or number < 0:
                continue
            result[("skins", str(skin["id"]))] = (
                f"https://ddragon.leagueoflegends.com/cdn/img/champion/loading/{alias}_{number}.jpg")
    return result


class ArtworkStore(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None, enabled=True, cache_directory=None):
        super().__init__(parent)
        self.enabled = enabled
        self.network = QNetworkAccessManager(self)
        # This manager is exclusively public, never shared with auth/client APIs.
        self.disk = QNetworkDiskCache(self)
        self.disk.setCacheDirectory(str(cache_directory or Path(APPDATA_DIR) / "artwork-cache"))
        self.disk.setMaximumCacheSize(96 * 1024 * 1024)
        self.network.setCache(self.disk)
        self.sources = {"lol": {}, "valorant": {}}
        self.names = {}
        self.started = set()
        self.pending = set()
        self.failed = set()
        self.queue = deque()
        self.active = 0
        self.replies = {}
        self.images = OrderedDict()
        self.thumbnails = OrderedDict()
        self.placeholders = {}

    def retry(self):
        """An explicit refresh also retries artwork after a network outage."""
        self.failed.clear()
        self.started.clear()
        self.changed.emit()

    def _json(self, url, callback):
        if not self.enabled:
            return
        self._enqueue(url, callback, 12 * 1024 * 1024)

    def _enqueue(self, url, callback, limit=3 * 1024 * 1024):
        if not public_asset_url(url) or url in self.pending or url in self.failed:
            return
        self.pending.add(url)
        self.queue.append((url, callback, limit))
        self._pump()

    def _pump(self):
        while self.active < 4 and self.queue:
            url, callback, limit = self.queue.popleft()
            request = QNetworkRequest(QUrl(url))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            request.setAttribute(QNetworkRequest.Attribute.CookieLoadControlAttribute,
                                 QNetworkRequest.LoadControl.Manual)
            request.setAttribute(QNetworkRequest.Attribute.CookieSaveControlAttribute,
                                 QNetworkRequest.LoadControl.Manual)
            request.setTransferTimeout(10000)
            request.setRawHeader(QByteArray(b"User-Agent"), QByteArray(b"RiotAuth-Artwork/1"))
            reply = self.network.get(request)
            self.replies[url] = reply
            self.active += 1
            reply.downloadProgress.connect(
                lambda received, total, r=reply, maximum=limit:
                self._progress(r, received, total, maximum))
            reply.finished.connect(lambda r=reply, u=url, c=callback, maximum=limit:
                                   self._finished(r, u, c, maximum))

    def _progress(self, reply, received, total, limit):
        if not sip.isdeleted(self) and not sip.isdeleted(reply) and (received > limit or total > limit):
            reply.abort()

    def _finished(self, reply, url, callback, limit):
        # Cached replies may be destroyed before a queued callback runs. Ignore
        # duplicate/old completions without consuming a newer request's slot.
        if sip.isdeleted(self) or self.replies.get(url) is not reply:
            return
        del self.replies[url]
        self.active -= 1
        self.pending.discard(url)
        if sip.isdeleted(reply):
            self.failed.add(url)
            self._pump()
            return
        body = bytes(reply.readAll()) if reply.error() == QNetworkReply.NetworkError.NoError else b""
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        reply.deleteLater()
        if status == 200 and body and len(body) <= limit:
            try:
                callback(body)
            except (ValueError, KeyError, TypeError, AttributeError):
                self.failed.add(url)
        else:
            self.failed.add(url)
        self._pump()

    def _ensure_catalog(self, game):
        if game in self.started or not self.enabled:
            return
        self.started.add(game)
        if game == "lol":
            def versions(body):
                version = json.loads(body)[0]
                if not re.fullmatch(r"[0-9.]+", version):
                    return
                def champions(payload):
                    self.sources["lol"].update(league_artwork(json.loads(payload)["data"], version))
                    self.changed.emit()
                self._json(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/championFull.json", champions)
            self._json("https://ddragon.leagueoflegends.com/api/versions.json", versions)
        elif game == "valorant":
            self._json("https://valorant-api.com/v1/agents", self._agents)
            self._json("https://valorant-api.com/v1/weapons", self._weapons)

    def _remember(self, collection, identifier, name, url):
        if public_asset_url(url):
            self.sources["valorant"][(collection, str(identifier).lower())] = url
            if name:
                self.names[(collection, name.casefold())] = url

    def _agents(self, body):
        for agent in json.loads(body)["data"]:
            if agent.get("isPlayableCharacter"):
                self._remember("characters", agent["uuid"], agent["displayName"], agent.get("displayIcon"))
        self.changed.emit()

    def _weapons(self, body):
        for weapon in json.loads(body)["data"]:
            for skin in weapon.get("skins", []):
                url = skin.get("displayIcon") or next(
                    (x.get("displayIcon") for x in skin.get("levels", []) if x.get("displayIcon")), "")
                self._remember("skins", skin["uuid"], skin["displayName"], url)
                for level in skin.get("levels", []):
                    self._remember("skins", level["uuid"], level.get("displayName"), level.get("displayIcon") or url)
                for chroma in skin.get("chromas", []):
                    self._remember("skins", chroma["uuid"], chroma.get("displayName"), chroma.get("displayIcon") or url)
        self.changed.emit()

    def source(self, game, collection, row):
        explicit = row.get("icon_url")
        if public_asset_url(explicit):
            return explicit
        self._ensure_catalog(game)
        url = self.sources.get(game, {}).get((collection, str(row.get("id", "")).lower()), "")
        if not url and game == "valorant":
            for name in [row.get("name", ""), *row.get("aliases", [])]:
                url = self.names.get((collection, str(name).casefold()), "")
                if url:
                    break
        return url

    def _loaded(self, url, body):
        buffer = QBuffer()
        buffer.setData(QByteArray(body))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        dimensions = reader.size()
        if not dimensions.isValid() or dimensions.width() > 4096 or dimensions.height() > 4096:
            self.failed.add(url)
            return
        image = reader.read()
        if image.isNull():
            self.failed.add(url)
            return
        pixmap = QPixmap.fromImage(image)
        self.images[url] = pixmap
        while len(self.images) > 128:
            old, _ = self.images.popitem(last=False)
            for key in list(self.thumbnails):
                if key[0] == old:
                    del self.thumbnails[key]
        self.changed.emit()

    def thumbnail(self, game, collection, row, size):
        url = self.source(game, collection, row)
        contain = game == "valorant" and collection == "skins"
        key = (url, size.width(), size.height(), contain)
        if url in self.images:
            self.images.move_to_end(url)
            if key not in self.thumbnails:
                self.thumbnails[key] = self._tile(self.images[url], size, contain)
                while len(self.thumbnails) > 256:
                    self.thumbnails.popitem(last=False)
            return self.thumbnails[key]
        if url and self.enabled:
            self._enqueue(url, lambda body, u=url: self._loaded(u, body))
        placeholder_key = (size.width(), size.height())
        if placeholder_key not in self.placeholders:
            self.placeholders[placeholder_key] = self._tile(None, size, False)
        return self.placeholders[placeholder_key]

    @staticmethod
    def _tile(pixmap, size, contain):
        # Render at 2x for crisp thumbnails on Windows display scaling.
        output = QPixmap(size * 2)
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, output.width(), output.height(), 12, 12)
        painter.setClipPath(path)
        painter.fillRect(output.rect(), QColor("#24272b"))
        if pixmap is not None:
            target = output.size() - QSize(12, 12) if contain else output.size()
            scaled = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio if contain
                                   else Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((output.width() - scaled.width()) // 2,
                               round((output.height() - scaled.height()) * (0.5 if contain else 0.25)), scaled)
        else:
            painter.setPen(QColor("#737b85"))
            painter.drawText(output.rect(), Qt.AlignmentFlag.AlignCenter, "·")
        painter.end()
        output.setDevicePixelRatio(2)
        return output


class ArtworkPreview(QLabel):
    def __init__(self, store, game, collection, row, parent=None):
        super().__init__(parent)
        self.store, self.game, self.collection, self.row = store, game, collection, row
        self.preview_size = (QSize(110, 160) if game == "lol" and collection == "skins" else
                     QSize(340, 130) if collection == "skins" else QSize(120, 120))
        self.setFixedHeight(self.preview_size.height())
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(row.get("name", ""))
        store.changed.connect(self.refresh)
        self.refresh()

    @pyqtSlot()
    def refresh(self):
        self.setPixmap(self.store.thumbnail(self.game, self.collection, self.row, self.preview_size))
