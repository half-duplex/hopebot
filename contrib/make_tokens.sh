#!/usr/bin/env bash

set -eu
source .env

#testing=true
tokentype="attendee"
#tokentype="presenter"
#tokentype="volunteer"
count=500
conf="HOPE16"

#######################################

testing=${testing:-${HOPEBOT_TESTING:-false}}
[ "$testing" == "true" ] && count=1000
echo "Generating $count $conf $tokentype tokens (testing=$testing)..."

test_uuid="${HOPEBOT_TEST_UUID}" # random uuid sans last 3 digits
now="$(date +'%Y-%m-%d_%H%M%S_%N')"
test_str="$([ "$testing" == "true" ] && echo "_test" || echo "")"
filename_base="hopetokens_${tokentype}${test_str}_${now}"

type_prefix=""
[ "$tokentype" == "presenter" ] && type_prefix="P"
[ "$tokentype" == "volunteer" ] && type_prefix="V"
[ "$testing" == "true" ] && type_prefix="${type_prefix}T"


if [ "$testing" == "true" ] ; then
    gen_fn () {
        seq -w 0 999 | awk '{print "'"${conf}${type_prefix}-${test_uuid}"'"$0}'
    }
else
    gen_fn () {
        tr -cd '0-9A-F' </dev/urandom | fold -w30 | head -n"$count" \
          | sed -re 's/^(.{8})(.{4})(.{4})(.{4})(.{10})$/'"${conf}${type_prefix}"'-\1-\2-\3-\4-\5/'
    }
fi

gen_fn | while read token ; do
    echo -n "$token,"
    echo -n "$token" | sha256sum
done | cut -d' ' -f1 >"$filename_base.csv"

if [ "$testing" == "true" ] ; then
    echo "Test tokens: ${conf}${type_prefix}-${test_uuid}___ (any 3 numbers)"
else
    cut -d, -f1 <"$filename_base.csv" | zstd -19 >"$filename_base-tokens.txt.zst"
fi
cut -d, -f2 <"$filename_base.csv" >"$filename_base-hashes.txt"

echo "Done:"
ls -lh "${filename_base}"*
echo "Load into bot with: !load_tokens ${filename_base}-hashes.txt ${tokentype}"
