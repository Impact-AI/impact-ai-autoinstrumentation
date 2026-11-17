// SPDX-License-Identifier: Apache-2.0

package source

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// A small MessagePack reader (https://github.com/msgpack/msgpack/blob/master/spec.md) for the Datadog trace intake,
// standard library only. It reads a value into nil, bool, int64, uint64, float64, string, []byte, []any or mpMap.
// Extension values are read and dropped (nil). Lengths are checked against the bytes left before anything is
// allocated, and nesting is bounded, so a hostile payload cannot make it allocate more than its own size or recurse
// without end.

// mpMap is a MessagePack map in its wire order. Keys are strings in the v0.4 format and string-table indexes in v0.5.
type mpMap []mpEntry

type mpEntry struct {
	Key   any
	Value any
}

// get is the value of the string key k, if the map has it.
func (m mpMap) get(k string) (any, bool) {
	for _, e := range m {
		if s, ok := e.Key.(string); ok && s == k {
			return e.Value, true
		}
	}
	return nil, false
}

const (
	mpMaxDepth   = 64
	mpInitialCap = 64
)

var errMsgpackShort = errors.New("msgpack: the payload ends inside a value")

type mpReader struct {
	b   []byte
	off int
}

func decodeMsgpack(b []byte) (any, error) {
	r := &mpReader{b: b}
	v, err := r.value(0)
	if err != nil {
		return nil, err
	}
	if r.off != len(r.b) {
		return nil, fmt.Errorf("msgpack: %d bytes after the value", len(r.b)-r.off)
	}
	return v, nil
}

func (r *mpReader) need(n int) ([]byte, error) {
	if n < 0 || r.off+n > len(r.b) || r.off+n < r.off {
		return nil, errMsgpackShort
	}
	out := r.b[r.off : r.off+n]
	r.off += n
	return out, nil
}

func (r *mpReader) uint(n int) (uint64, error) {
	b, err := r.need(n)
	if err != nil {
		return 0, err
	}
	switch n {
	case 1:
		return uint64(b[0]), nil
	case 2:
		return uint64(binary.BigEndian.Uint16(b)), nil
	case 4:
		return uint64(binary.BigEndian.Uint32(b)), nil
	default:
		return binary.BigEndian.Uint64(b), nil
	}
}

func (r *mpReader) length(n int) (int, error) {
	v, err := r.uint(n)
	if err != nil {
		return 0, err
	}
	if v > uint64(len(r.b)-r.off) {
		// Every element takes at least one byte, so a count beyond the bytes left is a lie.
		return 0, errMsgpackShort
	}
	return int(v), nil
}

func (r *mpReader) value(depth int) (any, error) {
	if depth > mpMaxDepth {
		return nil, errors.New("msgpack: nested too deep")
	}
	head, err := r.need(1)
	if err != nil {
		return nil, err
	}
	c := head[0]
	switch {
	case c <= 0x7f:
		return int64(c), nil
	case c >= 0xe0:
		return int64(int8(c)), nil
	case c&0xf0 == 0x80:
		return r.mapOf(int(c&0x0f), depth)
	case c&0xf0 == 0x90:
		return r.arrayOf(int(c&0x0f), depth)
	case c&0xe0 == 0xa0:
		return r.str(int(c & 0x1f))
	}
	switch c {
	case 0xc0:
		return nil, nil
	case 0xc2:
		return false, nil
	case 0xc3:
		return true, nil
	case 0xc4, 0xc5, 0xc6:
		n, err := r.length(1 << (c - 0xc4))
		if err != nil {
			return nil, err
		}
		b, err := r.need(n)
		if err != nil {
			return nil, err
		}
		return append([]byte(nil), b...), nil
	case 0xc7, 0xc8, 0xc9: // ext 8, 16, 32: a type byte, then the data
		n, err := r.length(1 << (c - 0xc7))
		if err != nil {
			return nil, err
		}
		_, err = r.need(n + 1)
		return nil, err
	case 0xca:
		v, err := r.uint(4)
		return float64(math.Float32frombits(uint32(v))), err
	case 0xcb:
		v, err := r.uint(8)
		return math.Float64frombits(v), err
	case 0xcc, 0xcd, 0xce, 0xcf:
		v, err := r.uint(1 << (c - 0xcc))
		if err != nil {
			return nil, err
		}
		if v <= math.MaxInt64 {
			return int64(v), nil
		}
		return v, nil
	case 0xd0:
		v, err := r.uint(1)
		return int64(int8(v)), err
	case 0xd1:
		v, err := r.uint(2)
		return int64(int16(v)), err
	case 0xd2:
		v, err := r.uint(4)
		return int64(int32(v)), err
	case 0xd3:
		v, err := r.uint(8)
		return int64(v), err
	case 0xd4, 0xd5, 0xd6, 0xd7, 0xd8: // fixext 1, 2, 4, 8, 16
		_, err := r.need(1 + 1<<(c-0xd4))
		return nil, err
	case 0xd9, 0xda, 0xdb:
		n, err := r.length(1 << (c - 0xd9))
		if err != nil {
			return nil, err
		}
		return r.str(n)
	case 0xdc, 0xdd:
		n, err := r.length(2 << (c - 0xdc))
		if err != nil {
			return nil, err
		}
		return r.arrayOf(n, depth)
	case 0xde, 0xdf:
		n, err := r.length(2 << (c - 0xde))
		if err != nil {
			return nil, err
		}
		return r.mapOf(n, depth)
	}
	return nil, fmt.Errorf("msgpack: unknown type byte 0x%02x", c)
}

func (r *mpReader) str(n int) (any, error) {
	b, err := r.need(n)
	if err != nil {
		return nil, err
	}
	return string(b), nil
}

func (r *mpReader) arrayOf(n int, depth int) (any, error) {
	if n > len(r.b)-r.off {
		return nil, errMsgpackShort
	}
	// The count is checked against the bytes left, but nested arrays can each claim them all: grow with what is read.
	out := make([]any, 0, min(n, mpInitialCap))
	for range n {
		v, err := r.value(depth + 1)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

func (r *mpReader) mapOf(n int, depth int) (any, error) {
	if 2*n > len(r.b)-r.off {
		return nil, errMsgpackShort
	}
	out := make(mpMap, 0, min(n, mpInitialCap))
	for range n {
		k, err := r.value(depth + 1)
		if err != nil {
			return nil, err
		}
		v, err := r.value(depth + 1)
		if err != nil {
			return nil, err
		}
		out = append(out, mpEntry{Key: k, Value: v})
	}
	return out, nil
}

// asUint64 reads an unsigned integer as the tracers write ids: any MessagePack integer, or a float that holds one.
func asUint64(v any) (uint64, bool) {
	switch n := v.(type) {
	case uint64:
		return n, true
	case int64:
		return uint64(n), true
	case float64:
		if n >= 0 && n <= math.MaxUint64 && n == math.Trunc(n) {
			return uint64(n), true
		}
	}
	return 0, false
}

func asInt64(v any) (int64, bool) {
	switch n := v.(type) {
	case int64:
		return n, true
	case uint64:
		if n <= math.MaxInt64 {
			return int64(n), true
		}
	case float64:
		if n == math.Trunc(n) && n >= math.MinInt64 && n <= math.MaxInt64 {
			return int64(n), true
		}
	}
	return 0, false
}

func asFloat64(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int64:
		return float64(n), true
	case uint64:
		return float64(n), true
	}
	return 0, false
}
