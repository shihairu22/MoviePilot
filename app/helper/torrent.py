import datetime
import re
from pathlib import Path
from typing import Tuple, Optional, List, Union
from urllib.parse import unquote

from torrentool.api import Torrent

from app.core.config import settings
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.types import MediaType


class TorrentHelper:
    """
    种子帮助类
    """
    def download_torrent(self, url: str,
                         cookie: str = None,
                         ua: str = None,
                         referer: str = None,
                         proxy: bool = False) \
            -> Tuple[Optional[Path], Optional[Union[str, bytes]], Optional[str], Optional[list], Optional[str]]:
        """
        把种子下载到本地
        :return: 种子保存路径、种子内容、种子主目录、种子文件清单、错误信息
        """
        if url.startswith("magnet:"):
            return None, url, "", [], f"{url} 为磁力链接"
        # 请求种子文件
        req = RequestUtils(
            ua=ua,
            cookies=cookie,
            referer=referer,
            proxies=settings.PROXY if proxy else None
        ).get_res(url=url, allow_redirects=False)
        while req and req.status_code in [301, 302]:
            url = req.headers['Location']
            if url and url.startswith("magnet:"):
                return None, url, "", [], f"获取到磁力链接：{url}"
            req = RequestUtils(
                ua=ua,
                cookies=cookie,
                referer=referer,
                proxies=settings.PROXY if proxy else None
            ).get_res(url=url, allow_redirects=False)
        if req and req.status_code == 200:
            if not req.content:
                return None, None, "", [], "未下载到种子数据"
            # 读取种子文件名
            file_name = self.__get_url_torrent_filename(req, url)
            # 种子文件路径
            file_path = Path(settings.TEMP_PATH) / file_name
            # 解析内容格式
            if req.text and str(req.text).startswith("magnet:"):
                # 磁力链接
                return None, req.text, "", [], f"获取到磁力链接：{req.text}"
            elif req.text and "下载种子文件" in req.text:
                # 首次下载提示页面
                skip_flag = False
                try:
                    form = re.findall(r'<form.*?action="(.*?)".*?>(.*?)</form>', req.text, re.S)
                    if form:
                        action = form[0][0]
                        if not action or action == "?":
                            action = url
                        elif not action.startswith('http'):
                            action = StringUtils.get_base_url(url) + action
                        inputs = re.findall(r'<input.*?name="(.*?)".*?value="(.*?)".*?>', form[0][1], re.S)
                        if action and inputs:
                            data = {}
                            for item in inputs:
                                data[item[0]] = item[1]
                            # 改写req
                            req = RequestUtils(
                                ua=ua,
                                cookies=cookie,
                                referer=referer,
                                proxies=settings.PROXY if proxy else None
                            ).post_res(url=action, data=data)
                            if req and req.status_code == 200:
                                # 检查是不是种子文件，如果不是抛出异常
                                Torrent.from_string(req.content)
                                # 跳过成功
                                logger.info(f"触发了站点首次种子下载，已自动跳过：{url}")
                                skip_flag = True
                            elif req is not None:
                                logger.warn(f"触发了站点首次种子下载，且无法自动跳过，"
                                            f"返回码：{req.status_code}，错误原因：{req.reason}")
                            else:
                                logger.warn(f"触发了站点首次种子下载，且无法自动跳过：{url}")
                except Exception as err:
                    logger.warn(f"触发了站点首次种子下载，尝试自动跳过时出现错误：{err}，链接：{url}")

                if not skip_flag:
                    return None, None, "", [], "种子数据有误，请确认链接是否正确，如为PT站点则需手工在站点下载一次种子"

            if req.content:
                # 检查是不是种子文件，如果不是仍然抛出异常
                try:
                    # 保存到文件
                    file_path.write_bytes(req.content)
                    # 获取种子目录和文件清单
                    torrentinfo = Torrent.from_file(file_path)
                    # 获取目录名
                    folder_name = torrentinfo.name
                    # 获取文件清单
                    if len(torrentinfo.files) <= 1:
                        # 单文件种子
                        file_list = [torrentinfo.name]
                    else:
                        file_list = [fileinfo.name for fileinfo in torrentinfo.files]
                    # 成功拿到种子数据
                    return file_path, req.content, folder_name, file_list, ""
                except Exception as err:
                    logger.error(f"种子文件解析失败：{err}")
                # 种子数据仍然错误
                return None, None, "", [], "种子数据有误，请确认链接是否正确"
            # 返回失败
            return None, None, "", [], ""
        elif req is None:
            return None, None, "", [], "无法打开链接：%s" % url
        elif req.status_code == 429:
            return None, None, "", [], "触发站点流控，请稍后重试"
        else:
            return None, None, "", [], f"下载种子出错，状态码：{req.status_code}"

    @staticmethod
    def __get_url_torrent_filename(req, url: str) -> str:
        """
        从下载请求中获取种子文件名
        """
        if not req:
            return ""
        disposition = req.headers.get('content-disposition') or ""
        file_name = re.findall(r"filename=\"?(.+)\"?", disposition)
        if file_name:
            file_name = unquote(str(file_name[0].encode('ISO-8859-1').decode()).split(";")[0].strip())
            if file_name.endswith('"'):
                file_name = file_name[:-1]
        elif url and url.endswith(".torrent"):
            file_name = unquote(url.split("/")[-1])
        else:
            file_name = str(datetime.datetime.now())
        return file_name

    @staticmethod
    def sort_group_torrents(torrent_list: List[Context]) -> List[Context]:
        """
        对媒体信息进行排序、去重
        """
        if not torrent_list:
            return []

        # 排序函数，标题、站点、资源类型、做种数量
        def get_sort_str(_context):
            _meta = _context.meta_info
            _torrent = _context.torrent_info
            season_len = str(len(_meta.get_season_list())).rjust(2, '0')
            episode_len = str(len(_meta.get_episode_list())).rjust(4, '0')
            # 排序：标题、资源类型、站点、做种、季集
            return "%s%s%s%s" % (str(_torrent.title).ljust(100, ' '),
                                 str(_torrent.pri_order).rjust(3, '0'),
                                 str(_torrent.seeders).rjust(10, '0'),
                                 "%s%s" % (season_len, episode_len))

        # 匹配的资源中排序分组选最好的一个下载
        # 按站点顺序、资源匹配顺序、做种人数下载数逆序排序
        torrent_list = sorted(torrent_list, key=lambda x: get_sort_str(x), reverse=True)
        # 控重
        result = []
        _added = []
        # 排序后重新加入数组，按真实名称控重，即只取每个名称的第一个
        for context in torrent_list:
            # 控重的主链是名称、年份、季、集
            meta = context.meta_info
            media = context.media_info
            if media.type != MediaType.MOVIE:
                media_name = "%s%s" % (media.get_title_string(),
                                       meta.get_season_episode_string())
            else:
                media_name = media.get_title_string()
            if media_name not in _added:
                _added.append(media_name)
                result.append(context)

        return result

    @staticmethod
    def get_torrent_episodes(files: list) -> list:
        """
        从种子的文件清单中获取所有集数
        """
        episodes = []
        for file in files:
            if Path(file).suffix not in settings.RMT_MEDIAEXT:
                continue
            meta = MetaInfo(file)
            if not meta.begin_episode:
                continue
            episodes = list(set(episodes).union(set(meta.get_episode_list())))
        return episodes
