# region 导入
from msgspec import Struct

from .video import Image

# endregion


# region 数据模型
class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str | None:
        return (
            self.author.avatar_thumb.url_list[0]
            if self.author.avatar_thumb.url_list
            else None
        )

    @property
    def image_urls(self) -> list[str]:
        return [urls[0] for urls in self.image_url_candidates]

    @property
    def image_url_candidates(self) -> list[list[str]]:
        return [
            list(dict.fromkeys(image.url_list))
            for image in self.images
            if image.url_list
        ]

    @property
    def dynamic_urls(self) -> list[str]:
        return [urls[0] for urls in self.dynamic_url_candidates]

    @property
    def dynamic_url_candidates(self) -> list[list[str]]:
        return [urls for image in self.images if (urls := image.live_video_urls)]

    @property
    def live_photo_video_urls(self) -> list[str | None]:
        return [image.live_video_url for image in self.images if image.url_list]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] | None = None


# endregion
