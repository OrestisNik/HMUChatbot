from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import UserUtteranceReverted
from rasa_sdk.events import SlotSet
from neo4j import GraphDatabase
import os
import stanza
import logging
import requests
import time
import stanza
from dotenv import load_dotenv
from fuzzywuzzy import fuzz, process


# Initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()
# Initialize the stanza pipeline

try:
    nlp = stanza.Pipeline('el', processors='tokenize,mwt,pos,lemma')
    logger.info("Stanza pipeline initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Stanza pipeline: {str(e)}")
    nlp = None

# Dictionary with specific name corrections
exceptions = {
    'βιοιατρικη': 'Βιοϊατρική',
    'τεχνητή νοημοσύνη': 'Τεχνητή Νοημοσύνη',
    'μάθημα': 'μαθήματα',
    'μαθημα': 'μαθήματα',
    'μαθηματα': 'subject',
    'ορέστη': 'Ορέστης',
    'νίκο': 'Νίκος',
    'βιδάκη': 'Βιδάκης',
    'μανόλι': 'Μανόλης',
    'μανολή': 'Μανόλης',
    'τσικνάκη': 'Τσικνάκης',
    'μαρακάκη': 'Μαρακάκης',
    'μαλάμος': 'Μαλάμος',
    'μαλαμος': 'Μαλάμος',
    'μαλάμου': 'Μαλάμος',
    'μαλαμου': 'Μαλάμος',
    'ωρες γραφείου': 'ώρες γραφείου',
    'ώρες γραφειου': 'ώρες γραφείου',
}

# Attribute mapping from user-friendly names to database attribute names
attribute_mapping = {
    'ώρες γραφείου': 'ώρες_γραφείου',
    'τηλέφωνο': 'τηλέφωνο',
    'email': 'email',
    'όνομα': 'firstname',
    'επίθετο': 'lastname'
    # Add more mappings if necessary
}

def to_nominative(genitive_name: str) -> str:
    """
    Convert a Greek name from genitive to nominative case with enhanced handling.
    
    Args:
        genitive_name: Name in genitive case (e.g., "του Μαρακάκη")
    
    Returns:
        Name in nominative case (e.g., "Μαρακάκης")
    """
    if not genitive_name or not isinstance(genitive_name, str):
        return genitive_name
        
    try:
        # Handle common Greek surname patterns first
        patterns = [
            (r'([α-ωά-ώ]+)ης$', r'\1ης'),      # Παππάς remains Παππάς
            (r'([α-ωά-ώ]+)ου$', r'\1ος'),      # Παπαδόπουλου → Παπαδόπουλος
            (r'([α-ωά-ώ]+)ακη$', r'\1άκης'),   # Βιδάκη → Βιδάκης
            (r'([α-ωά-ώ]+)η$', r'\1ης')        # Αλεξίου → Αλεξίου
        ]
        
        # Try pattern matching first for common cases
        lower_name = genitive_name.lower()
        for pattern, replacement in patterns:
            if re.search(pattern, lower_name):
                nominative = re.sub(pattern, replacement, lower_name)
                return nominative.capitalize()
        
        # Fall back to Stanza for complex cases
        doc = nlp(genitive_name)
        lemmas = [word.lemma for sent in doc.sentences for word in sent.words 
                 if word.feats and 'Case=Gen' in word.feats]
        
        return lemmas[0].capitalize() if lemmas else genitive_name
        
    except Exception as e:
        logger.error(f"Name conversion error: {str(e)}")
        return genitive_name

def normalize_text(text: str) -> str:
    """
    Normalize Greek text by:
    - Converting to lowercase
    - Removing diacritics
    - Standardizing similar characters
    
    Args:
        text: Input string to normalize
        
    Returns:
        Normalized string
    """
    if not text:
        return text
    
    # Basic normalization
    normalized = text.lower().strip()
    
    # Handle common Greek variations
    replacements = {
        'ά': 'α', 'έ': 'ε', 'ή': 'η',
        'ί': 'ι', 'ό': 'ο', 'ύ': 'υ',
        'ώ': 'ω', 'ς': 'σ'
    }
    
    return ''.join(replacements.get(c, c) for c in normalized)

def fuzzy_match_name(input_name: str, names: List[str], threshold: int = 80) -> Optional[str]:
    """
    Find the best fuzzy match with configurable threshold.
    
    Args:
        input_name: Name to match
        names: List of candidate names
        threshold: Minimum similarity score (0-100)
        
    Returns:
        Best match or None if no good matches
    """
    if not names:
        return None
        
    normalized_input = normalize_text(input_name)
    normalized_names = [normalize_text(name) for name in names]
    
    # Get matches with scores
    matches = process.extract(
        normalized_input,
        normalized_names,
        scorer=fuzz.token_set_ratio,  # Better for name matching
        limit=5
    )
    
    # Return best match if above threshold
    best_match, score = matches[0] if matches else (None, 0)
    return names[normalized_names.index(best_match)] if score >= threshold else None

class Neo4jConnection:
    def __init__(self, uri, user, password):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self._driver.close()

    def query(self, query, parameters=None):
        with self._driver.session() as session:
            result = session.run(query, parameters)
            return [record for record in result]

# Load environment variables
load_dotenv()

class ActionGetSpecificProfessorInfo(Action):
    """Custom action to retrieve specific professor attributes from Neo4j"""

    def name(self) -> Text:
        return "action_get_specific_professor_info"

    async def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any]
    ) -> List[Dict[Text, Any]]:

        try:
            professor_name = tracker.get_slot('professor_name')
            requested_attribute = tracker.get_slot('attribute')

            if not professor_name:
                dispatcher.utter_message(text="Παρακαλώ δώστε το επώνυμο του καθηγητή.")
                return []

            if not requested_attribute:
                dispatcher.utter_message(
                    text="Ποια συγκεκριμένα στοιχεία θέλετε; (π.χ. email, τηλέφωνο, ώρες γραφείου, μαθήματα)")
                return []

            nominative_name = self._to_nominative(professor_name)
            logger.info(f"Converted '{professor_name}' to nominative: '{nominative_name}'")

            with GraphDatabase.driver(
                os.getenv("NEO4J_URI"),
                auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
            ) as driver:
                with driver.session() as session:
                    result = session.run("MATCH (p:Professor) RETURN p.lastname AS lastname")
                    db_names = [record["lastname"] for record in result]

                    if not db_names:
                        dispatcher.utter_message(text="Η βάση δεδομένων καθηγητών είναι προσωρινά μη διαθέσιμη.")
                        return []

                    best_match = self._fuzzy_match_name(nominative_name, db_names)
                    if not best_match:
                        logger.warning(f"No match found for '{nominative_name}' in {db_names}")
                        dispatcher.utter_message(text=f"Δεν βρέθηκε καθηγητής με επώνυμο {professor_name}.")
                        return []

                    normalized_attribute = requested_attribute.lower().strip()

                    # Handle subjects separately via relationship
                    if normalized_attribute in ['μαθήματα', 'μαθηματα', 'subject', 'subjects']:
                        subjects = self._get_professor_subjects(session, best_match)
                        if subjects:
                            response = f"Ο καθηγητής {best_match} διδάσκει τα εξής μαθήματα:\n" + "\n".join(
                                f"- {subject}" for subject in subjects)
                        else:
                            response = f"Δεν βρέθηκαν μαθήματα για τον καθηγητή {best_match}."
                        dispatcher.utter_message(text=response)
                        return [SlotSet('professor_name', best_match)]

                    # Map standard attributes
                    property_name = self._map_attribute_to_property(normalized_attribute)
                    if not property_name:
                        dispatcher.utter_message(text=f"Δεν υποστηρίζεται η ιδιότητα '{requested_attribute}'.")
                        return []

                    result = session.run(
                        f"MATCH (p:Professor {{lastname: $name}}) RETURN p.{property_name} AS value",
                        {"name": best_match}
                    )
                    record = result.single()

                    if not record or record["value"] is None:
                        dispatcher.utter_message(
                            text=f"Δεν βρέθηκαν στοιχεία {requested_attribute} για τον καθηγητή {best_match}."
                        )
                    else:
                        response = self._format_response(best_match, requested_attribute, record["value"])
                        dispatcher.utter_message(text=response)

                    return [SlotSet('professor_name', best_match)]

        except Exception as e:
            logger.error(f"Error processing request: {str(e)}", exc_info=True)
            dispatcher.utter_message(text="Συγγνώμη, προέκυψε σφάλμα κατά την επεξεργασία του αιτήματός σας.")
            return []

    def _get_professor_subjects(self, session, lastname: str) -> List[str]:
        result = session.run("""
        MATCH (p:Professor {lastname: $lastname})-[:TEACHES]->(s:Subject)
        RETURN s.name AS subject_name
        ORDER BY subject_name
        """, {"lastname": lastname})
        return [record["subject_name"] for record in result] if result else []

    def _map_attribute_to_property(self, attribute: str) -> Optional[str]:
        attr = attribute.lower().strip()
        if attr in ['μαθήματα', 'μαθηματα', 'subject', 'subjects']:
            return None  # Prevent property access for relational data
        return {
            'τηλέφωνο': 'phone',
            'phone': 'phone',
            'email': 'email',
            'ηλεκτρονική διεύθυνση': 'email',
            'ώρες γραφείου': 'office_hours',
            'ωρες γραφειου': 'office_hours',
            'office hours': 'office_hours'
        }.get(attr)

    def _format_response(self, lastname: str, attribute: str, value: str) -> str:
        display_names = {
            'phone': 'Τηλέφωνο',
            'email': 'Email',
            'office_hours': 'Ώρες γραφείου'
        }
        display_name = display_names.get(attribute.lower(), attribute)
        return f"{lastname} - {display_name}: {value}"

    def _to_nominative(self, genitive_name: str) -> str:
        if not genitive_name or not isinstance(genitive_name, str):
            return genitive_name

        try:
            patterns = [
                (r'([α-ωά-ώ]+)ης$', r'\1ης'),
                (r'([α-ωά-ώ]+)ου$', r'\1ος'),
                (r'([α-ωά-ώ]+)ακη$', r'\1άκης'),
                (r'([α-ωά-ώ]+)η$', r'\1ης')
            ]
            lower_name = genitive_name.lower()
            for pattern, replacement in patterns:
                if re.search(pattern, lower_name):
                    nominative = re.sub(pattern, replacement, lower_name)
                    return nominative.capitalize()

            if nlp:
                doc = nlp(genitive_name)
                lemmas = [word.lemma for sent in doc.sentences for word in sent.words
                          if word.feats and 'Case=Gen' in word.feats]
                if lemmas:
                    return lemmas[0].capitalize()

            return genitive_name.capitalize()

        except Exception as e:
            logger.warning(f"Name conversion failed for '{genitive_name}': {str(e)}")
            return genitive_name.capitalize()

    def _normalize_text(self, text: str) -> str:
        if not text:
            return text
        replacements = {
            'ά': 'α', 'έ': 'ε', 'ή': 'η',
            'ί': 'ι', 'ό': 'ο', 'ύ': 'υ',
            'ώ': 'ω', 'ς': 'σ'
        }
        return ''.join(replacements.get(c, c) for c in text.lower().strip())

    def _fuzzy_match_name(self, input_name: str, names: List[str], threshold: int = 85) -> Optional[str]:
        if not names:
            return None
        normalized_input = self._normalize_text(input_name)
        normalized_names = [self._normalize_text(name) for name in names]
        matches = process.extract(
            normalized_input,
            normalized_names,
            scorer=fuzz.token_set_ratio,
            limit=5
        )
        best_match, score = matches[0] if matches else (None, 0)
        return names[normalized_names.index(best_match)] if score >= threshold else None

        
class Neo4jConnection:
    def __init__(self, uri, user, password):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self._driver.close()

    def query(self, query, parameters=None):
        with self._driver.session() as session:
            result = session.run(query, parameters)
            return [record for record in result]  # Materialize results immediately
            


from typing import Any, Dict, List, Optional, Text
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet
from neo4j import GraphDatabase
from fuzzywuzzy import fuzz, process
import os
import logging
import stanza
import re

# Initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Initialize Stanza pipeline for Greek
try:
    nlp = stanza.Pipeline('el', processors='tokenize,mwt,pos,lemma')
    logger.info("Stanza pipeline initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Stanza pipeline: {str(e)}")
    nlp = None

class ActionGetSpecificProfessorInfo(Action):
    """Custom action to retrieve specific professor attributes from Neo4j"""
    
    def name(self) -> Text:
        return "action_get_specific_professor_info"

    async def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any]
    ) -> List[Dict[Text, Any]]:
        
        try:
            # Get slots
            professor_name = tracker.get_slot('professor_name')
            requested_attribute = tracker.get_slot('attribute')

            # Validate inputs
            if not professor_name:
                dispatcher.utter_message(text="Παρακαλώ δώστε το επώνυμο του καθηγητή.")
                return []
                
            if not requested_attribute:
                dispatcher.utter_message(text="Ποια συγκεκριμένα στοιχεία θέλετε; (π.χ. email, τηλέφωνο, ώρες γραφείου)")
                return []

            # Convert to nominative case
            nominative_name = self._to_nominative(professor_name)
            logger.info(f"Converted '{professor_name}' to nominative: '{nominative_name}'")

            with GraphDatabase.driver(
                os.getenv("NEO4J_URI", "neo4j+s://165e87b5.databases.neo4j.io"),
                auth=(
                    os.getenv("NEO4J_USER", "neo4j"),
                    os.getenv("NEO4J_PASSWORD", "11RA8yKX_sKle_Fy99sFjILmnVFbYxeHm3ltkp6aKV8")
                )
            ) as driver:
                with driver.session() as session:
                    # Get all professor names from database
                    result = session.run("MATCH (p:Professor) RETURN p.lastname AS lastname")
                    db_names = [record["lastname"] for record in result]
                    
                    if not db_names:
                        dispatcher.utter_message(text="Η βάση δεδομένων καθηγητών είναι προσωρινά μη διαθέσιμη.")
                        return []

                    # Find best match using fuzzy matching
                    best_match = self._fuzzy_match_name(nominative_name, db_names)
                    if not best_match:
                        logger.warning(f"No match found for '{nominative_name}' in {db_names}")
                        dispatcher.utter_message(text=f"Δεν βρέθηκε καθηγητής με επώνυμο {professor_name}.")
                        return []

                    # Get requested attribute
                    professor_info = session.run(
                        f"MATCH (p:Professor {{lastname: $name}}) RETURN p.{self._map_attribute(requested_attribute)} AS value",
                        {"name": best_match}
                    ).single()

                    if not professor_info or not professor_info["value"]:
                        dispatcher.utter_message(
                            text=f"Δεν βρέθηκαν στοιχεία {requested_attribute} για τον καθηγητή {best_match}."
                        )
                    else:
                        response = self._format_response(best_match, requested_attribute, professor_info["value"])
                        dispatcher.utter_message(text=response)
                    
                    return [SlotSet('professor_name', best_match)]
                    
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}", exc_info=True)
            dispatcher.utter_message(text="Συγγνώμη, προέκυψε σφάλμα κατά την επεξεργασία του αιτήματός σας.")
            return []

    def _to_nominative(self, genitive_name: str) -> str:
        """Convert Greek name from genitive to nominative case"""
        if not genitive_name or not isinstance(genitive_name, str):
            return genitive_name
            
        try:
            # Common Greek surname patterns
            patterns = [
                (r'([α-ωά-ώ]+)ης$', r'\1ης'),      # e.g., Παππάς
                (r'([α-ωά-ώ]+)ου$', r'\1ος'),      # e.g., Παπαδόπουλου → Παπαδόπουλος
                (r'([α-ωά-ώ]+)ακη$', r'\1άκης'),   # e.g., Βιδάκη → Βιδάκης
                (r'([α-ωά-ώ]+)η$', r'\1ης')        # e.g., Αλεξίου
            ]
            
            # Try pattern matching first
            lower_name = genitive_name.lower()
            for pattern, replacement in patterns:
                if re.search(pattern, lower_name):
                    nominative = re.sub(pattern, replacement, lower_name)
                    return nominative.capitalize()
            
            # Fall back to Stanza for complex cases
            if nlp:
                doc = nlp(genitive_name)
                lemmas = [word.lemma for sent in doc.sentences for word in sent.words 
                         if word.feats and 'Case=Gen' in word.feats]
                if lemmas:
                    return lemmas[0].capitalize()
                    
            return genitive_name.capitalize()
            
        except Exception as e:
            logger.warning(f"Name conversion failed for '{genitive_name}': {str(e)}")
            return genitive_name.capitalize()

    def _normalize_text(self, text: str) -> str:
        """Normalize Greek text for fuzzy matching"""
        if not text:
            return text
            
        # Convert to lowercase and remove diacritics
        replacements = {
            'ά': 'α', 'έ': 'ε', 'ή': 'η',
            'ί': 'ι', 'ό': 'ο', 'ύ': 'υ',
            'ώ': 'ω', 'ς': 'σ'
        }
        return ''.join(replacements.get(c, c) for c in text.lower().strip())

    def _fuzzy_match_name(self, input_name: str, names: List[str], threshold: int = 85) -> Optional[str]:
        """Find best fuzzy match with configurable threshold"""
        if not names:
            return None
            
        normalized_input = self._normalize_text(input_name)
        normalized_names = [self._normalize_text(name) for name in names]
        
        matches = process.extract(
            normalized_input,
            normalized_names,
            scorer=fuzz.token_set_ratio,
            limit=5
        )
        
        best_match, score = matches[0] if matches else (None, 0)
        return names[normalized_names.index(best_match)] if score >= threshold else None

    def _map_attribute(self, attribute: str) -> str:
        """Map user-friendly attributes to database properties"""
        attribute_map = {
            'τηλέφωνο': 'τηλέφωνο',
            'phone': 'τηλέφωνο',
            'email': 'email',
            'ηλεκτρονική διεύθυνση': 'email',
            'ώρες γραφείου': 'ώρες_γραφείου',
            'ωρες γραφειου': 'ώρες_γραφείου',
            'office hours': 'ώρες_γραφείου'
        }
        return attribute_map.get(attribute.lower(), attribute)

    def _format_response(self, lastname: str, attribute: str, value: str) -> str:
        """Format the response in Greek"""
        attribute_names = {
            'τηλέφωνο': 'Τηλέφωνο',
            'phone': 'Τηλέφωνο',
            'email': 'Email',
            'ηλεκτρονική διεύθυνση': 'Email',
            'ώρες γραφείου': 'ώρες_γραφείου',
            'ωρες γραφειου': 'ώρες_γραφείου',
            'office hours': 'ώρες_γραφείου'
        }
        # Get the display name, defaulting to original attribute if not found
        display_name = attribute_names.get(attribute.lower(), attribute)
    
        # Special case: Remove underscore only for display
        if attribute.lower() in ['ώρες_γραφείου', 'ώρες γραφείου', 'office hours']:
            display_name = 'Ώρες γραφείου'
    
        return f"{lastname} - {display_name}: {value}"

# Initialize logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
class ActionGetClassroomInfo(Action):

    def name(self) -> Text:
        return "action_get_classroom_info"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        classroom_name = tracker.get_slot('classroom')
        # Log slot values for debugging
        logger.debug(f"Classroom name: {classroom_name}")

        if not classroom_name:
            dispatcher.utter_message(response="utter_ask_classroom")
            return []

        classroom_info = self.get_classroom_info(classroom_name)

        if classroom_info:
            dispatcher.utter_message(text=f"Τα μαθήματα που διδάσκονται στην αίθουσα {classroom_name} είναι: {classroom_info}.")
        else:
            dispatcher.utter_message(text=f"Συγγνώμη, δεν βρέθηκε καμία πληροφορία για την αίθουσα: {classroom_name}.")

        return [SlotSet("classroom", classroom_name)]

    def get_classroom_info(self, classroom_name: Text) -> Any:
        # Read Neo4j connection details from environment variables
        uri = os.getenv('NEO4J_URI', 'neo4j+s://165e87b5.databases.neo4j.io')
        user = os.getenv('NEO4J_USERNAME', 'neo4j')
        password = os.getenv('NEO4J_PASSWORD', '11RA8yKX_sKle_Fy99sFjILmnVFbYxeHm3ltkp6aKV8')

        # Connect to the Neo4j database
        driver = GraphDatabase.driver(uri, auth=(user, password))

        classroom_info = None

        # Log connection details for debugging (do not log sensitive information in production)
        logger.debug(f"Connecting to Neo4j with URI: {uri}")

        with driver.session() as session:
            # Log the query execution
            logger.debug(f"Executing query for classroom: {classroom_name}")

            result = session.run("""
            MATCH (subject:Subject)-[:TEACHED_IN]->(classroom:Classroom {name: $classroom_name})
            RETURN collect(subject.name) AS subjects_taught
            """, classroom_name=classroom_name)

            record = result.single()

            if record and record['subjects_taught']:
                classroom_info = ", ".join(record['subjects_taught'])
                # Log the found information for subjects
                logger.debug(f"Found subjects taught in {classroom_name}: {classroom_info}")
            else:
                logger.debug(f"No subjects found for classroom: {classroom_name}")

        driver.close()
        return classroom_info

class ActionGetLecturesInfo(Action):

    def name(self) -> Text:
        return "action_get_lectures_info"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        lesson = tracker.get_slot('lesson')
        classroom = tracker.get_slot('classroom')

        # Log slot values for debugging
        logger.debug(f"Desired lesson: {lesson}")
        logger.debug(f"Desired classroom: {classroom}")

        lectures_info = self.get_lectures_info(lesson, classroom)

        if lectures_info:
            dispatcher.utter_message(text=f"Η αίθουσα διδασκαλίας του μαθήματος {lesson} είναι: {lectures_info}.")
        else:
            dispatcher.utter_message(text=f"Συγγνώμη, δε βρέθηκε η πληροφορία: {lesson}.")

        return [SlotSet("classroom", classroom), SlotSet("lesson", lesson)]

    def get_lectures_info(self, lesson: Text, classroom: Text) -> Any:
        # Read Neo4j connection details from environment variables
        uri = os.getenv('NEO4J_URI', 'neo4j+s://165e87b5.databases.neo4j.io')
        user = os.getenv('NEO4J_USERNAME', 'neo4j')
        password = os.getenv('NEO4J_PASSWORD', '11RA8yKX_sKle_Fy99sFjILmnVFbYxeHm3ltkp6aKV8')

        # Connect to the Neo4j database
        driver = GraphDatabase.driver(uri, auth=(user, password))
        lecture_info = None

        # Log connection details for debugging (do not log sensitive information in production)
        logger.debug(f"Connecting to Neo4j with URI: {uri}")

        with driver.session() as session:
            # Log the query execution
            logger.debug(f"Executing query for lesson: {lesson}")

            result = session.run("""
            MATCH (subject:Subject {name: $lesson})-[:TEACHED_IN]->(classroom:Classroom)
            RETURN classroom.name AS classroom_name
            """, lesson=lesson)

            record = result.single()

            if record:
                lecture_info = record.get("classroom_name")
                # Log the found information
                logger.debug(f"Found classroom for lesson '{lesson}': {lecture_info}")
            else:
                logger.debug(f"No classroom found for lesson: {lesson}")

        driver.close()
        return lecture_info



class ActionDefaultFallback(Action):
    def name(self):
        return "action_default_fallback"

    async def run(self, dispatcher, tracker, domain):
        # Send the default message
        dispatcher.utter_message(response="utter_default")

        # Optionally, revert back to the previous user message
        return [UserUtteranceReverted()]
