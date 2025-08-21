#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
from math import ceil, log2
from os.path import exists as path_exists
from secrets import choice
from string import punctuation, whitespace
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from hashlib import _Hash as Hash


class ConfToken(str):
    @property
    def sha256(self) -> Hash:
        return sha256(self.encode("utf8"))


def gen_token(
    prefix: str,
    separator: str = "-",
    groups_of: int = 5,
    entropy_bits: int = 69,
    characters="abcdefghijklmnopqrstuvxyz",
) -> ConfToken:
    """Generate one formatted token.

    :param prefix: e.g. "HOPE_16::"
    :param separator: Character to divide sections
    :param groups_of: How many characters per section
    :param entropy_bits: How many bits of random data per token
    :param characters: The set of characters to choose from

    The defaults are chosen to produce a short but easy-to-type random part,
    like "adibc-culrx-kxcki".

    69 bits of entropy means an attacker making one guess per second for 7
    days (604800 attempts) has around a 1-in-100bn chance of guessing one of
    10,000 valid tokens (604800/1e4/2**69).
    """

    min_group_size = 3
    characters = tuple(set(characters))  # ensure uniqueness
    bits_per_char = log2(len(characters))
    char_count = ceil(entropy_bits / bits_per_char)

    token = ""
    while char_count > 0:
        this_group_size = (
            groups_of if char_count >= groups_of + min_group_size else char_count
        )
        token += "".join(choice(characters) for _ in range(this_group_size))
        char_count -= this_group_size
        if char_count > 0:
            token += separator

    if "rn" in token or "vv" in token:
        # retry: rn is ambiguous with m, vv with w
        return gen_token(prefix, separator, groups_of, entropy_bits)
    return ConfToken(prefix + token)


def gen_tokens(count: int, *args, **kwargs) -> set[ConfToken]:
    tokens: set[ConfToken] = set()
    while len(tokens) < count:  # ensure no duplicates (at least in this set)
        tokens.add(gen_token(*args, **kwargs))
    return tokens


def cli():
    parser = argparse.ArgumentParser(
        description="Generate tokens for HOPE CoreBot",
    )
    parser.add_argument("prefix", type=str, help='The token prefix, e.g. "HOPE_16::"')
    parser.add_argument(
        "--count", "-n", type=int, required=True, help="How many tokens to generate"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,  # FileType doesn't let us refuse to overwrite
        help="File to write the generated tokens to",
    )
    parser.add_argument(
        "--bits",
        type=int,
        default=69,
        help="How many bits of entropy in each token",
    )
    parser.add_argument(
        "--group",
        type=int,
        default=5,
        help="How many characters per group (abcde-fghij-…)",
    )
    parser.add_argument(
        "--characters",
        type=str,
        default="abcdefghijklmnopqrstuvxyz",
        help="The set of characters to choose from",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite a pre-existing output file"
    )

    args = parser.parse_args()

    if args.output:
        output_file = args.output
    else:
        clean_prefix = args.prefix.replace("/", "_").rstrip(punctuation + whitespace)
        # Uses system timezone to match expectations as best we can
        output_file = "tokens_{}_{}.txt".format(
            clean_prefix, datetime.now().strftime("%Y%m%d-%H%M%S")
        )
    if path_exists(output_file) and not args.overwrite:
        print("Cowardly refusing to overwrite {}".format(output_file))
        exit(2)

    print("Writing {} {} tokens to {}...".format(args.count, args.prefix, output_file))
    tokens = gen_tokens(
        args.count, args.prefix, groups_of=args.group, entropy_bits=args.bits
    )
    open(output_file, "w").writelines(t + "\n" for t in tokens)
    print('Load into the bot with e.g. "!load_tokens {}"'.format(output_file))


if __name__ == "__main__":
    cli()
