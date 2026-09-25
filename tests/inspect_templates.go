// Evaluate production inspect templates with the same Go template engine as Docker.
package main

import (
	"encoding/json"
	"os"
	"text/template"
)

func main() {
	var input struct {
		Templates []string
		Labels    map[string]string
	}
	if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil {
		panic(err)
	}
	data := struct {
		Id, Name string
		Config   struct {
			Image  string
			Labels map[string]string
		}
		State           struct{ Running bool }
		HostConfig      struct{ Runtime string }
		Mounts          []string
		NetworkSettings struct{ Networks map[string]string }
	}{}
	data.Config.Labels = input.Labels
	functions := template.FuncMap{"json": func(value interface{}) (string, error) {
		encoded, err := json.Marshal(value)
		return string(encoded), err
	}}
	for _, source := range input.Templates {
		t, err := template.New("inspect").Funcs(functions).Parse(source)
		if err != nil {
			panic(err)
		}
		if err := t.Execute(os.Stdout, data); err != nil {
			panic(err)
		}
		os.Stdout.WriteString("\n")
	}
}
