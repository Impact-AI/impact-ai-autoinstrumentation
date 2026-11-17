// SPDX-License-Identifier: Apache-2.0

package forward

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRoutesAreReadFromTheFileTheSwitchWrites(t *testing.T) {
	doc := `{"routes":[
	  {"name":"shop-checkout","listen":":14318","protocol":"otlp-http","source":"vendor","target":"https://otlp.vendor.example/v1/traces"},
	  {"name":"shop-agent","listen":":14317","protocol":"otlp-grpc","source":"vendor","target":"https://otlp.vendor.example:4317"},
	  {"name":"shop-llm","listen":":14319","protocol":"otlp-http","source":"langfuse","base":true,"target":"https://llm.vendor.example"},
	  {"name":"shop-legacy","listen":"127.0.0.1:19411","protocol":"relay","target":"http://zipkin.vendor.example:9411","decode":"zipkin"}
	]}`
	path := filepath.Join(t.TempDir(), "routes.json")
	if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	routes, err := LoadRoutes(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(routes) != 4 || routes[1].Protocol != ProtocolOTLPGRPC || !routes[2].Base || routes[2].Source != "langfuse" || routes[3].Decode != "zipkin" {
		t.Fatalf("routes %+v", routes)
	}
	if got := routes[0].TargetHost(); got != "otlp.vendor.example" {
		t.Errorf("TargetHost %q", got)
	}
	if got := routes[1].TargetHost(); got != "otlp.vendor.example:4317" {
		t.Errorf("TargetHost %q", got)
	}
}

func TestBadRoutesAreRefusedWithTheirName(t *testing.T) {
	route := func(fields string) string { return `{"routes":[{` + fields + `}]}` }
	cases := map[string]struct{ doc, want string }{
		"no routes":             {`{"routes":[]}`, "no routes"},
		"unknown field":         {route(`"name":"a","listen":":1","protocol":"relay","target":"http://v.example","extra":1`), "unknown field"},
		"bad name":              {route(`"name":"A_B","listen":":1","protocol":"relay","target":"http://v.example"`), "name"},
		"bad listen":            {route(`"name":"a","listen":"nowhere","protocol":"relay","target":"http://v.example"`), `route "a": listen`},
		"bad protocol":          {route(`"name":"a","listen":":1","protocol":"thrift","target":"http://v.example"`), `route "a": protocol`},
		"no host":               {route(`"name":"a","listen":":1","protocol":"relay","target":"http:///x"`), `route "a": target`},
		"decode on otlp":        {route(`"name":"a","listen":":1","protocol":"otlp-http","source":"vendor","target":"http://v.example","decode":"zipkin"`), `route "a": decode`},
		"unknown decoder":       {route(`"name":"a","listen":":1","protocol":"relay","target":"http://v.example","decode":"thrift"`), `route "a": decode`},
		"grpc with a path":      {route(`"name":"a","listen":":1","protocol":"otlp-grpc","target":"http://v.example/x"`), `route "a": target`},
		"otlp without a source": {route(`"name":"a","listen":":1","protocol":"otlp-http","target":"http://v.example/v1/traces"`), `route "a": source`},
		"grpc without a source": {route(`"name":"a","listen":":1","protocol":"otlp-grpc","target":"http://v.example"`), `route "a": source`},
		"bad source":            {route(`"name":"a","listen":":1","protocol":"otlp-http","source":"Vendor X","target":"http://v.example"`), `route "a": source`},
		"source on a relay":     {route(`"name":"a","listen":":1","protocol":"relay","source":"zipkin","target":"http://v.example"`), `route "a": source`},
		"base on a relay":       {route(`"name":"a","listen":":1","protocol":"relay","base":true,"target":"http://v.example"`), `route "a": base`},
		"base on grpc":          {route(`"name":"a","listen":":1","protocol":"otlp-grpc","source":"vendor","base":true,"target":"http://v.example"`), `route "a": base`},
		"same name": {`{"routes":[{"name":"a","listen":":1","protocol":"relay","target":"http://v.example"},` +
			`{"name":"a","listen":":2","protocol":"relay","target":"http://v.example"}]}`, "twice"},
		"same listen": {`{"routes":[{"name":"a","listen":":1","protocol":"relay","target":"http://v.example"},` +
			`{"name":"b","listen":":1","protocol":"relay","target":"http://v.example"}]}`, "twice"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := ParseRoutes([]byte(tc.doc))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %v, want one that says %q", err, tc.want)
			}
		})
	}
}
