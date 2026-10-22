# region 导入
from random import choice

from msgspec import Struct, field

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
            choice(self.author.avatar_thumb.url_list)
            if self.author.avatar_thumb.url_list
            else None
        )

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images if image.url_list]

    @property
    def dynamic_urls(self) -> list[str]:
        return [image.live_video_url for image in self.images if image.live_video_url]

    @property
    def live_photo_video_urls(self) -> list[str | None]:
        return [image.live_video_url for image in self.images if image.url_list]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)


# endregion
