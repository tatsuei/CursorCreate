from io import BytesIO
from typing import BinaryIO, Iterator, Tuple, Set, Dict, Any
from format_core import AnimatedCursorStorageFormat, to_int, to_bytes
from PIL import BmpImagePlugin
from cur_format import CurFormat
from cursor import CursorIcon, Cursor, AnimatedCursor


# UTILITY METHODS:


def read_chunks(buffer: BinaryIO, skip_chunks: Set[str]=None, byteorder="little") -> Iterator[Tuple[bytes, int, bytes]]:
    if(skip_chunks is None):
        skip_chunks = set()

    while(True):
        next_id = buffer.read(4)
        if(next_id == b''):
            return
        if(next_id in skip_chunks):
            continue

        size = to_int(buffer.read(4), byteorder=byteorder)
        yield (next_id, size, buffer.read(size))


def write_chunk(buffer: BinaryIO, chunk_id: bytes, chunk_data: bytes, byteorder="little"):
    buffer.write(chunk_id[:4])
    buffer.write(to_bytes(len(chunk_data), 4, byteorder=byteorder))
    buffer.write(chunk_data)


def _header_chunk(header: None, data: bytes, data_out: Dict[str, Any]):
    if(header is not None):
        raise SyntaxError("This ani has 2 headers!")

    if(len(data) == 36):
        data = data[4:]

    h_data = {
        "num_frames": to_int(data[0:4]),
        "num_steps": to_int(data[4:8]),
        "width": to_int(data[8:12]),
        "height": to_int(data[12:16]),
        "bit_count": to_int(data[16:20]),
        "num_planes": 1,
        "display_rate": to_int(data[24:28]),
        "contains_seq": bool((to_int(data[28:32]) >> 1) & 1),
        "is_in_ico": bool(to_int(data[28:32]) & 1)
    }

    data_out["header"] = h_data
    data_out["seq"] = [i % h_data["num_frames"] for i in range(h_data["num_steps"])]
    data_out["rate"] = [h_data["display_rate"]] * h_data["num_steps"]


def _list_chunk(header: Dict[str, Any], data: bytes, data_out: Dict[str, Any]):
    if(header is None):
        raise SyntaxError("LIST chunk became before header!")

    if(data[:4] != b"fram"):
        raise SyntaxError("LIST chunk should start with 'fram'!")

    data = data[4:]
    cursor_list = []

    for chunk_id, size, chunk_data in read_chunks(BytesIO(data)):
        if(chunk_id == b"icon"):
            if(header["is_in_ico"]):
                # Cursors are stored as either .cur or .ico, use CurFormat to read them...
                cursor_list.append(CurFormat.read(BytesIO(chunk_data)))
            else:
                # BMP format, load in and then correct the height...
                c_icon = CursorIcon(BmpImagePlugin.DibImageFile(BytesIO(chunk_data)), 0, 0)
                c_icon.image._size = (c_icon.image.size[0], c_icon.image.size[1] // 2)
                d, e, o, a = c_icon.image.tile[0]
                c_icon.image.tile[0] = d, (0, 0) + c_icon.image.size, o, a
                # Add the image to the cursor list...
                cursor_list.append(Cursor([c_icon]))

    data_out["list"] = cursor_list


def _seq_chunk(header: Dict[str, Any], data: bytes, data_out: Dict[str, Any]):
    if(header is None):
        raise SyntaxError("seq chunk came before header!")

    if((len(data) // 4) != header["num_steps"]):
        raise SyntaxError("Length of sequence chunk does not match the number of steps!")

    data_out["seq"] = [to_int(data[i:i+4]) for i in range(0, len(data), 4)]


def _rate_chunk(header: Dict[str, Any], data: bytes, data_out: Dict[str, Any]):
    if(header is None):
        raise SyntaxError("rate chunk became before header!")

    if((len(data) // 4) != header["num_steps"]):
        raise SyntaxError("Length of rate chunk does not match the number of steps!")

    data_out["rate"] = [to_int(data[i:i+4]) for i in range(0, len(data), 4)]


class AniFormat(AnimatedCursorStorageFormat):
    RIFF_MAGIC = b"RIFF"
    ACON_MAGIC = b"ACON"

    CHUNKS = {
        b"anih": _header_chunk,
        b"LIST": _list_chunk,
        b"rate": _rate_chunk,
        b"seq ": _seq_chunk
    }

    @classmethod
    def check(cls, first_bytes) -> bool:
        return ((first_bytes[:4] == cls.RIFF_MAGIC) and (first_bytes[8:12] == cls.ACON_MAGIC))

    @classmethod
    def read(cls, cur_file: BinaryIO) -> AnimatedCursor:
        magic_header = cur_file.read(12)

        if(not cls.check(magic_header)):
            raise SyntaxError("Not a .ani file!")

        ani_data: Dict[str, Any] = {"header": None}

        for chunk_id, chunk_len, chunk_data in read_chunks(cur_file):
            if(chunk_id in cls.CHUNKS):
                cls.CHUNKS[chunk_id](ani_data["header"], chunk_data, ani_data)

        ani_cur = AnimatedCursor()

        for idx, rate in zip(ani_data["seq"], ani_data["rate"]):
            # We have to convert the rate to milliseconds. Normally stored in jiffies(1/60ths of a second)
            ani_cur.append((ani_data["list"][idx], int((rate * 1000) / 60)))

        return ani_cur


    DEF_CURSOR_SIZE = (32, 32)

    @classmethod
    def write(cls, cursor: AnimatedCursor, out: BinaryIO):
        # Write the magic...
        out.write(cls.RIFF_MAGIC)
        # We will deal with writing the length of the entire file later...
        out.write(b"\0\0\0\0")
        out.write(cls.ACON_MAGIC)

        # Write the header...
        header = bytearray(36)
        # We write the header length twice for some dumb reason...
        header[0:4] = to_bytes(36, 4)  # Header length...
        header[4:8] = to_bytes(len(cursor), 4)  # Number of frames
        header[8:12] = to_bytes(len(cursor), 4)  # Number of steps
        # Ignore width, height, bit count, and number of planes....
        header[28:32] = to_bytes(10, 4)  # We just pass 10 as the default delay...
        header[32:36] = to_bytes(1, 4)  # The flags, we just want the last flag flipped which states if data is stored in .cur...

        write_chunk(out, b"anih", header)

        # Write the LIST of icons...
        list_data = bytearray(b"fram")
        delay_data = bytearray()

        for sub_cursor, delay in cursor:
            # Writing a single cursor to the list...
            mem_stream = BytesIO()
            CurFormat.write(sub_cursor, mem_stream)
            # We write these chunks manually to avoid wasting a ton of space, as using "write_chunks" ends up being
            # just as complicated...
            cur_data = mem_stream.getvalue()
            list_data.extend(b"icon")
            list_data.extend(to_bytes(len(cur_data), 4))
            list_data.extend(cur_data)
            # Writing the delay to the rate chunk
            delay_data.extend(to_bytes(round((delay * 60) / 1000), 4))

        # Now that we have gathered the data actually write the chunks...
        write_chunk(out, b"LIST", list_data)
        write_chunk(out, b"rate", delay_data)

        # Now we are to the end, get the length of the file and write it as the RIFF chunk length...
        entire_file_len = out.tell() - 8
        out.seek(4)
        out.write(to_bytes(entire_file_len, 4))