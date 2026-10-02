(require '[clojure.test :as test]
         '[clojure.java.io :as io]
         '[clojure.string :as str])

(def root (io/file "/workspace/.cache/penpot/backend/test"))
(def namespaces
  (->> (file-seq root)
       (filter #(and (.isFile %) (str/ends-with? (.getName %) "_test.clj")))
       (map #(second (re-find #"\(ns\s+([^\s\)]+)" (slurp %))))
       (remove nil?)
       (map symbol)
       sort
       vec))
(def selected
  (if-let [pattern (System/getenv "PGFIRE_TEST_PATTERN")]
    (filterv #(re-find (re-pattern pattern) (str %)) namespaces)
    namespaces))
(when (empty? selected)
  (throw (ex-info "No upstream Penpot tests selected" {})))
(println "Loading upstream Penpot test namespaces:" (count selected))
(doseq [namespace selected]
  (println "LOAD" namespace)
  (require namespace))
(let [started (System/nanoTime)
      result (apply test/run-tests selected)
      seconds (/ (- (System/nanoTime) started) 1e9)
      report (assoc result :namespaces (mapv str selected) :seconds seconds)]
  (spit (or (System/getenv "PGFIRE_TEST_REPORT") "/tmp/pgfire-test-result.edn") (pr-str report))
  (println "PGFIRE_TEST_RESULT" (pr-str report))
  (shutdown-agents)
  (System/exit (if (zero? (+ (:fail result) (:error result))) 0 1)))
