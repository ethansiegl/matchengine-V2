node {
    stage('Checkout') {
        checkout scm

        //parent wrapper image
        docker.image('mongo:3.6.10').withRun("-e \"MONGO_INITDB_ROOT_USERNAME=root\" -e \"MONGO_INITDB_ROOT_PASSWORD=password\"") { c ->

            stage("spin up db") {
                //get access to mongoshell methods
                docker.image('mongo:3.6.10').inside("--link ${c.id}") {

                    sh "env"
                    //wait until mongodb is initialized
                    sh "bash -c 'COUNTER=0 && until mongo mongodb://root:password@${c.id}:27017/matchminer --eval \"print(\\\"waited for connection\\\")\"; do sleep 1; let \"COUNTER++\"; echo \$COUNTER; [ \$COUNTER -eq 15 ] && exit 1 ; done'"

                    stage("load test data") {
                        sh "mongorestore --gzip --uri mongodb://root:password@${c.id}:27017/matchminer --dir=tests/data/integration_data"
                    }
                }
            }

            //use api test image
            stage("run tests") {
                docker.image('python:3.7').inside("--link ${c.id}") {
                    sh """
                       cat << 'EOF' > SECRETS_JSON.json
{
                      "MONGO_HOST": "${c.id.substring(0, 12)}",
                      "MONGO_PORT": 27017,
                      "MONGO_USERNAME": "root",
                      "MONGO_PASSWORD": "password",
                      "MONGO_RO_USERNAME": "root",
                      "MONGO_RO_PASSWORD": "password",
                      "MONGO_DBNAME": "matchminer",
                      "MONGO_AUTH_SOURCE": ""
}

                   """

                    sh "cat SECRETS_JSON.json"

                    sh """
                       pip install -r requirements.txt && \
                       export SECRETS_JSON=SECRETS_JSON.json && \
                       nosetests -v --with-xunit tests
                       """

                    //report on nosetests results
                    junit "*.xml"
                }
            }
        }
    }
}
