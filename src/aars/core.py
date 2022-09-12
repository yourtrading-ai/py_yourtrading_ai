from abc import ABC
import asyncio

from aleph_client.types import Account
from pydantic import BaseModel
from typing import Type, TypeVar, Dict, ClassVar, List, Optional, Set, Any, Union

import aleph_client.asynchronous as client
from aleph_client.chains.ethereum import get_fallback_account

from src.aars.exceptions import AlreadyForgottenError

FALLBACK_ACCOUNT = get_fallback_account()
AARS_TEST_CHANNEL = "AARS_TEST"

T = TypeVar('T', bound='AlephRecord')


class Record(BaseModel, ABC):
    """
    A basic record which is persisted on Aleph decentralized storage.

    Can be updated and forgotten. Revision numbers begin at 0 (original upload) and increment for each `upsert()` call.

    Previous revisions can be restored by calling `fetch_revision(rev_no=<number>)` or `fetch_revision(
    rev_hash=<item_hash of inserted update>)`.

    Records have an `indices` class attribute, which allows one to select an index and query it with a key.
    """
    forgotten: bool = False
    item_hash: str = None
    current_revision: int = None
    revision_hashes: List[str] = []
    __indices: ClassVar[Dict[str, 'Index']] = {}

    def __repr__(self):
        return f'{type(self).__name__}({self.item_hash})'

    @property
    def content(self) -> Dict[str, Any]:
        """
        :return: content dictionary of the object, as it is to be stored on Aleph.
        """
        return self.dict(exclude={'item_hash', 'current_revision', 'revision_hashes', 'indices', 'forgotten'})

    async def update_revision_hashes(self: T):
        self.revision_hashes = [self.item_hash] + await fetch_revisions(type(self), ref=self.item_hash)

    async def fetch_revision(self: T, rev_no: int = None, rev_hash: str = None) -> T:
        """
        Fetches a revision of the object by revision number (0 => original) or revision hash.
        :param rev_no: the revision number of the revision to fetch.
        :param rev_hash: the hash of the revision to fetch.
        """
        if rev_no is not None:
            if rev_no < 0:
                rev_no = len(self.revision_hashes) + rev_no
            if self.current_revision == rev_no:
                return self
            elif rev_no > len(self.revision_hashes):
                raise IndexError(f'No revision no. {rev_no} found for {self}')
            else:
                self.current_revision = rev_no
        elif rev_hash is not None:
            try:
                self.current_revision = self.revision_hashes.index(rev_hash)
            except ValueError:
                raise IndexError(f'{rev_hash} is not a revision of {self}')
        else:
            raise ValueError('Either rev or hash must be provided')

        # always fetch from aleph
        self.__dict__.update((await fetch_records(type(self), item_hashes=[self.item_hash]))[0].content)

        return self

    async def upsert(self):
        await post_or_amend_object(self)
        if self.current_revision == 0:
            [index.add(self) for index in self.__indices.values()]
        return self

    async def forget(self):
        if not self.forgotten:
            await forget_object(self)
            self.forgotten = True
        else:
            raise AlreadyForgottenError(self)

    @classmethod
    async def create(cls: Type[T], **kwargs) -> T:
        obj = cls(**kwargs)
        return await obj.upsert()

    @classmethod
    async def from_post(cls: Type[T], post: Dict[str, Any]) -> T:
        obj = cls(**post['content'])
        if post.get('ref') is None:
            obj.item_hash = post['item_hash']
            obj.revision_hashes.append(post['item_hash'])
        else:
            obj.item_hash = post['ref']
            obj.revision_hashes = await fetch_revisions(cls, ref=obj.item_hash)
        obj.item_hash = post['item_hash'] if post.get('ref') is None else post['ref']
        await obj.update_revision_hashes()
        obj.current_revision = obj.revision_hashes.index(post['item_hash'])
        return obj

    @classmethod
    async def fetch(cls: Type[T], hashes: Union[str, List[str]]) -> List[T]:
        if not isinstance(hashes, List):
            hashes = [hashes]
        return await fetch_records(cls, list(hashes))

    @classmethod
    async def fetch_all(cls: Type[T]) -> List[T]:
        return await fetch_records(cls)

    @classmethod
    async def query(cls: Type[T], keys: Union[str, List[str]], index: Optional[str] = 'item_hash') -> List[T]:
        if not isinstance(keys, List):
            keys = [keys]
        if cls.__indices.get(index) is None:
            raise ValueError(f'No index "{index}" found for {cls}')
        return await cls.__indices[index].fetch(keys)

    @classmethod
    def add_index(cls: Type[T], index: 'Index') -> None:
        cls.__indices[repr(index)] = index


class Index(Record):
    # TODO: multi-key index
    """
    Class to define Indices.
    """
    datatype: Type[T]
    index_on: str
    hashmap: Dict[str, str] = {}

    def __init__(self, datatype: Type[T], index_on: str):
        super(Index, self).__init__(datatype=datatype, index_on=index_on)
        datatype.add_index(self)

    def __str__(self):
        return f"Index({self.datatype.__name__}.{self.index_on})"

    def __repr__(self):
        return f"{self.datatype.__name__}.{self.index_on}"

    async def fetch(self, keys: List[str] = None) -> List[Record]:
        hashes: Set[str]
        if keys is None:
            hashes = set(self.hashmap.values())
        else:
            hashes = set([self.hashmap[key] for key in keys])

        return await fetch_records(self.datatype, list(hashes))

    def add(self, obj: T):
        assert isinstance(obj, Record)
        self.hashmap[getattr(obj, self.index_on)] = obj.item_hash


async def post_or_amend_object(obj: T, account=None, channel=None):
    """
    Posts or amends an object to Aleph. If the object is already posted, it's ref is updated.
    :param obj: The object to post or amend.
    :param account: The account to post the object with. If None, will use the fallback account.
    :param channel: The channel to post the object to. If None, will use the TEST channel of the object.
    :return: The object, as it is now on Aleph.
    """
    if account is None:
        account = FALLBACK_ACCOUNT
    if channel is None:
        channel = AARS_TEST_CHANNEL
    name = type(obj).__name__
    resp = await client.create_post(account, obj.content, post_type=name, channel=channel, ref=obj.item_hash)
    if obj.item_hash is None:
        obj.item_hash = resp['item_hash']
    obj.revision_hashes.append(resp['item_hash'])
    obj.current_revision = len(obj.revision_hashes) - 1


async def forget_object(obj: T, account: Account = None, channel: str = None):
    """
    Deletes an object from Aleph.
    :param obj: The object to delete.
    :param account: The account to delete the object with. If None, will use the fallback account.
    :param channel: The channel to delete the object from. If None, will use the TEST channel of the object.
    """
    if account is None:
        account = FALLBACK_ACCOUNT
    if channel is None:
        channel = AARS_TEST_CHANNEL
    hashes = [obj.item_hash] + obj.revision_hashes
    await client.forget(account, hashes, reason=None, channel=channel)


async def fetch_records(datatype: Type[T],
                        item_hashes: List[str] = None,
                        channel: str = None,
                        owner: str = None) -> List[T]:
    """Retrieves posts as objects by its aleph item_hash.
    :param datatype: The type of the objects to retrieve.
    :param item_hashes: Aleph item_hashes of the objects to fetch.
    :param channel: Channel in which to look for it.
    :param owner: Account that owns the object."""
    assert issubclass(datatype, Record)
    channels = None if channel is None else [channel]
    owners = None if owner is None else [owner]
    if item_hashes is None and channels is None and owners is None:
        channels = [AARS_TEST_CHANNEL]
    resp = await client.get_posts(hashes=item_hashes, channels=channels, types=[datatype.__name__], addresses=owners)
    tasks = [datatype.from_post(post) for post in resp['posts']]
    return list(await asyncio.gather(*tasks))


async def fetch_revisions(datatype: Type[T],
                          ref: str,
                          channel: str = None,
                          owner: str = None) -> List[str]:
    """Retrieves posts of revisions of an object by its item_hash.
    :param datatype: The type of the objects to retrieve.
    :param ref: item_hash of the object, whose revisions to fetch.
    :param channel: Channel in which to look for it.
    :param owner: Account that owns the object."""
    owners = None if owner is None else [owner]
    channels = None if channel is None else [channel]
    if owners is None and channels is None:
        channels = [AARS_TEST_CHANNEL]
    resp = await client.get_posts(refs=[ref], channels=channels, types=[datatype.__name__], addresses=owners)
    return list(reversed([post['item_hash'] for post in resp['posts']]))  # reverse to get the oldest first
